import os
import uuid
import json
import base64
import requests
import gzip
import websockets
from datetime import datetime
from core.utils.util import check_model_key
from core.providers.tts.base import TTSProviderBase
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        if config.get("appid"):
            self.appid = int(config.get("appid"))
        else:
            self.appid = ""
        self.access_token = config.get("access_token")
        self.cluster = config.get("cluster")

        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice")

        self.api_url = config.get("api_url")
        self.authorization = config.get("authorization")
        self.header = {"Authorization": f"{self.authorization}{self.access_token}"}
        
        # WebSocket API configuration
        self.ws_host = config.get("ws_host", "openspeech.bytedance.com")
        self.ws_api_url = config.get("ws_api_url", f"wss://{self.ws_host}/api/v1/tts/ws_binary")
        self.ws_header = {"Authorization": f"{self.authorization}{self.access_token}"}
        
        check_model_key("TTS", self.access_token)

    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    async def text_to_speak(self, text, output_file):
        request_json = {
            "app": {
                "appid": f"{self.appid}",
                "token": self.access_token,
                "cluster": self.cluster,
            },
            "user": {"uid": "1"},
            "audio": {
                "voice_type": self.voice,
                "encoding": "wav",
                "speed_ratio": 1.0,
                "volume_ratio": 1.0,
                "pitch_ratio": 1.0,
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": text,
                "text_type": "plain",
                "operation": "query",
                "with_frontend": 1,
                "frontend_type": "unitTson",
            },
        }

        try:
            resp = requests.post(
                self.api_url, json.dumps(request_json), headers=self.header
            )
            if "data" in resp.json():
                data = resp.json()["data"]
                file_to_save = open(output_file, "wb")
                file_to_save.write(base64.b64decode(data))
            else:
                raise Exception(
                    f"{__name__} status_code: {resp.status_code} response: {resp.content}"
                )
        except Exception as e:
            raise Exception(f"{__name__} error: {e}")
    
    async def generate_streaming(self, text, **kwargs):
        """生成流式音频数据，使用WebSocket连接进行流式TTS
        
        直接使用ogg_opus格式，避免格式转换
        """
        # 创建请求JSON
        request_json = {
            "app": {
                "appid": self.appid,
                "token": self.access_token,
                "cluster": self.cluster
            },
            "user": {
                "uid": str(uuid.uuid4())
            },
            "audio": {
                "voice_type": self.voice,
                "encoding": "ogg_opus",  # 直接使用opus格式
                "speed_ratio": 1.0,
                "volume_ratio": 1.0,
                "pitch_ratio": 1.0,
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": text,
                "text_type": "plain",
                "operation": "submit"  # 使用submit操作以获取流式响应
            }
        }
        
        # 根据字节跳动TTS协议构造WebSocket二进制消息
        # version: b0001 (4 bits)
        # header size: b0001 (4 bits)
        # message type: b0001 (Full client request) (4bits)
        # message type specific flags: b0000 (none) (4bits)
        # message serialization method: b0001 (JSON) (4 bits)
        # message compression: b0001 (gzip) (4bits)
        # reserved data: 0x00 (1 byte)
        default_header = bytearray(b'\x11\x10\x11\x00')
        
        try:
            # 压缩请求负载
            payload_bytes = str.encode(json.dumps(request_json))
            payload_bytes = gzip.compress(payload_bytes)
            
            # 构造完整请求
            full_client_request = bytearray(default_header)
            full_client_request.extend((len(payload_bytes)).to_bytes(4, 'big'))  # payload size(4 bytes)
            full_client_request.extend(payload_bytes)  # payload
            
            logger.bind(tag=TAG).info(f"正在连接豆包TTS WebSocket API...")
            
            # 连接WebSocket服务器
            async with websockets.connect(self.ws_api_url, extra_headers=self.ws_header, ping_interval=None) as ws:
                # 发送请求
                await ws.send(full_client_request)
                
                # 持续接收响应直到完成
                while True:
                    try:
                        res = await ws.recv()
                        
                        # 解析响应头
                        header_size = res[0] & 0x0f
                        message_type = res[1] >> 4
                        message_type_specific_flags = res[1] & 0x0f
                        payload = res[header_size*4:]
                        
                        # 处理音频响应
                        if message_type == 0xb:  # audio-only server response
                            if message_type_specific_flags == 0:  # ACK
                                continue
                                
                            # 解析音频数据
                            sequence_number = int.from_bytes(payload[:4], "big", signed=True)
                            payload_size = int.from_bytes(payload[4:8], "big", signed=False)
                            audio_chunk = payload[8:]
                            
                            # 直接返回opus格式音频数据
                            yield audio_chunk
                            
                            # 如果是最后一个数据包，结束循环
                            if sequence_number < 0:
                                break
                                
                        # 处理错误消息
                        elif message_type == 0xf:  # error message
                            code = int.from_bytes(payload[:4], "big", signed=False)
                            msg_size = int.from_bytes(payload[4:8], "big", signed=False)
                            error_msg = payload[8:]
                            
                            # 解压错误消息
                            message_compression = res[2] & 0x0f
                            if message_compression == 1:
                                error_msg = gzip.decompress(error_msg)
                                
                            error_msg = str(error_msg, "utf-8")
                            logger.bind(tag=TAG).error(f"豆包TTS错误: 代码={code}, 消息={error_msg}")
                            break
                            
                    except Exception as e:
                        logger.bind(tag=TAG).error(f"处理TTS响应出错: {str(e)}")
                        break
                        
        except Exception as e:
            logger.bind(tag=TAG).error(f"豆包TTS WebSocket连接失败: {str(e)}")
            raise