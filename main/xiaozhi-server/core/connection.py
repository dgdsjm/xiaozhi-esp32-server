import os
import copy
import json
import uuid
import time
import queue
import asyncio
import traceback
from config.config_loader import get_private_config_from_api
from config.manage_api_client import DeviceNotFoundException, DeviceBindException
from core.utils.output_counter import add_device_output
import threading
import websockets
from typing import Dict, Any
from plugins_func.loadplugins import auto_import_modules
from config.logger import setup_logging
from core.utils.dialogue import Message, Dialogue
from core.handle.textHandle import handleTextMessage
from core.utils.util import (
    get_string_no_punctuation_or_emoji,
    extract_json_from_string,
    get_ip_info,
    initialize_modules,
)
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from core.handle.receiveAudioHandle import handleAudioMessage
from core.handle.functionHandler import FunctionHandler
from plugins_func.register import Action, ActionResponse
from core.auth import AuthMiddleware, AuthenticationError
from core.mcp.manager import MCPManager
from config.config_loader import (
    get_private_config_from_api,
    DeviceNotFoundException,
    DeviceBindException,
)

TAG = __name__

auto_import_modules("plugins_func.functions")


class TTSException(RuntimeError):
    pass


class ConnectionHandler:
    def __init__(
        self, config: Dict[str, Any], _vad, _asr, _llm, _tts, _memory, _intent
    ):
        self.config = copy.deepcopy(config)
        self.logger = setup_logging()
        self.auth = AuthMiddleware(config)

        self.need_bind = False
        self.bind_code = None

        self.websocket = None
        self.headers = None
        self.client_ip = None
        self.client_ip_info = {}
        self.session_id = None
        self.prompt = None
        self.welcome_msg = None

        # 客户端状态相关
        self.client_abort = False
        self.client_listen_mode = "auto"
        self.is_speaking = False
        self.should_stop_speaking = False

        # 线程任务相关
        self.loop = asyncio.get_event_loop()
        self.stop_event = threading.Event()
        self.tts_queue = queue.Queue()
        self.audio_play_queue = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=10)

        # 依赖的组件
        self.vad = _vad
        self.asr = _asr
        self.llm = _llm
        self.tts = _tts
        self.memory = _memory
        self.intent = _intent

        # vad相关变量
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_have_voice_last_time = 0.0
        self.client_no_voice_last_time = 0.0
        self.client_voice_stop = False

        # asr相关变量
        self.asr_audio = []
        self.asr_server_receive = True

        # llm相关变量
        self.llm_finish_task = False
        self.dialogue = Dialogue()

        # tts相关变量
        self.tts_first_text_index = -1
        self.tts_last_text_index = -1

        # iot相关变量
        self.iot_descriptors = {}
        self.func_handler = None

        self.cmd_exit = self.config["exit_commands"]
        self.max_cmd_length = 0
        for cmd in self.cmd_exit:
            if len(cmd) > self.max_cmd_length:
                self.max_cmd_length = len(cmd)

        self.close_after_chat = False  # 是否在聊天结束后关闭连接
        self.use_function_call_mode = False
        # 添加Gemini Live API支持 - 细分控制
        self.use_gemini_live = False
        self.gemini_live_audio_input = False
        self.gemini_live_audio_output = False
    
        # 检查LLM类型是否为Gemini
        if hasattr(self.llm, '__class__') and self.llm.__class__.__name__ == 'LLMProvider' and hasattr(self.llm, 'use_live_api'):
            # 从LLM实例直接获取配置
            self.use_gemini_live = self.llm.use_live_api
            self.gemini_live_audio_input = getattr(self.llm, 'live_audio_input', False)
            self.gemini_live_audio_output = getattr(self.llm, 'live_audio_output', False)
            
            # 检查是否启用了任何live特性
            if self.use_gemini_live or self.gemini_live_audio_input or self.gemini_live_audio_output:
                self.logger.bind(tag=TAG).info(f"检测到Gemini Live API配置：音频输入={self.gemini_live_audio_input}, 音频输出={self.gemini_live_audio_output}")
        
        # 初始化相关资源
        self.gemini_live_session = None
        self.gemini_response_queue = None
        self.audio_response_task = None

    async def handle_connection(self, ws):
        try:
            # 获取并验证headers
            self.headers = dict(ws.request.headers)
            # 获取客户端ip地址
            self.client_ip = ws.remote_address[0]
            self.logger.bind(tag=TAG).info(
                f"{self.client_ip} conn - Headers: {self.headers}"
            )

            # 进行认证
            await self.auth.authenticate(self.headers)

            # 认证通过,继续处理
            self.websocket = ws
            self.session_id = str(uuid.uuid4())
             # 如果启用了Gemini Live API的任一功能，创建会话
            if self.use_gemini_live and hasattr(self.llm, "create_live_session"):
                self.gemini_response_queue = asyncio.Queue()
                self.gemini_live_session = await self.llm.create_live_session()
                if self.gemini_live_session:
                    # 创建响应处理任务
                    self.audio_response_task = asyncio.create_task(self._process_gemini_responses())
                    mode_str = []
                    if self.gemini_live_audio_input: mode_str.append("语音输入")
                    if self.gemini_live_audio_output: mode_str.append("语音输出")
                    self.logger.bind(tag=TAG).info(f"已创建Gemini Live会话，模式: {'+'.join(mode_str) or '仅文本'}")

            self.welcome_msg = self.config["xiaozhi"]
            self.welcome_msg["session_id"] = self.session_id
            await self.websocket.send(json.dumps(self.welcome_msg))

            # 异步初始化
            self.executor.submit(self._initialize_components)
            # tts 消化线程
            self.tts_priority_thread = threading.Thread(
                target=self._tts_priority_thread, daemon=True
            )
            self.tts_priority_thread.start()

            # 音频播放 消化线程
            self.audio_play_priority_thread = threading.Thread(
                target=self._audio_play_priority_thread, daemon=True
            )
            self.audio_play_priority_thread.start()

            try:
                async for message in self.websocket:
                    try:
                        # 检查消息类型
                        if isinstance(message, str):
                            try:
                                data = json.loads(message)
                                message_type = data.get("type", "")
                                
                                if message_type == "text":
                                    await self.handle_text_message(ws, data)
                                elif message_type == "audio":
                                    await self.handle_audio_message(ws, data)
                                elif message_type == "stopTTS":
                                    await self.handle_stop_tts(ws)
                                else:
                                    # 使用现有的文本处理函数处理其他文本消息
                                    await handleTextMessage(self, message)
                            except json.JSONDecodeError:
                                # 非JSON文本消息，使用现有的文本处理逻辑
                                await handleTextMessage(self, message)
                        elif isinstance(message, bytes):
                            # 二进制数据，使用现有的音频处理逻辑
                            await handleAudioMessage(self, message)
                    except Exception as e:
                        self.logger.bind(tag=TAG).error(f"Error processing message: {str(e)}")
            except websockets.exceptions.ConnectionClosed:
                self.logger.bind(tag=TAG).info("客户端断开连接")

        except AuthenticationError as e:
            self.logger.bind(tag=TAG).error(f"Authentication failed: {str(e)}")
            return
        except Exception as e:
            stack_trace = traceback.format_exc()
            self.logger.bind(tag=TAG).error(f"Connection error: {str(e)}-{stack_trace}")
            return
        finally:
            await self._save_and_close(ws)

    async def _save_and_close(self, ws):
        """保存记忆并关闭连接"""
        try:
            await self.memory.save_memory(self.dialogue.dialogue)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"保存记忆失败: {e}")
        finally:
            await self.close(ws)

    async def handle_text_message(self, websocket, data):
        """处理文本消息"""
        text = data.get("payload", "")
        if not text:
            return
            
        self.logger.bind(tag=TAG).info(f"Received text: {text}")
        
        # 处理文本请求并获得回复
        if self.use_function_call_mode:
            await self.chat_with_function_calling(text)
        else:
            await self.chat(text)
    
    
    async def _process_gemini_responses(self):
        """处理来自Gemini Live API的响应（文本或音频）"""
        try:
            self.logger.bind(tag=TAG).info("启动Gemini响应处理任务")
            while True:
                if not self.gemini_live_session or not self.websocket:
                    await asyncio.sleep(0.5)
                    continue
                    
                # 获取下一个响应或等待
                try:
                    turn = await asyncio.wait_for(self.gemini_response_queue.get(), 0.5)
                    
                    # 如果启用了音频输出，则处理为TTS流
                    has_audio_output = self.gemini_live_audio_output
                    if has_audio_output:
                        # 发送TTS开始状态
                        await self.send_tts_status(self.websocket, "start", "")
                        self.is_speaking = True
                        self.asr_server_receive = False  # 播放时停止接收音频
                    
                    # 处理响应
                    transcript = ""
                    async for response in turn:
                        if self.should_stop_speaking:
                            break
                            
                        # 处理音频数据 (如果启用了语音输出)
                        if has_audio_output and response.data:
                            try:
                                await self.websocket.send(response.data)
                                await asyncio.sleep(0.01)
                            except Exception as e:
                                self.logger.bind(tag=TAG).error(f"发送音频数据失败: {e}")
                                break
                        
                        # 处理文本响应
                        if response.text:
                            transcript += response.text
                    
                    # 如果没有语音输出或同时有文本，发送文本响应
                    if transcript:
                        # 发送完整转录文本
                        await self.websocket.send(json.dumps({
                            "type": "textResponse",
                            "payload": transcript,
                            "session_id": self.session_id
                        }))
                        
                        # 如果没有语音输出但有文本，则用传统TTS播放
                        if not has_audio_output:
                            await self.stream_tts_response(self.websocket, transcript)
                    
                    # 发送TTS结束状态 (如果启用了语音输出)
                    if has_audio_output:
                        await self.send_tts_status(self.websocket, "stop")
                        # 重置状态
                        self.is_speaking = False
                        self.asr_server_receive = True
                    
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"处理Gemini响应出错: {e}")
                    self.is_speaking = False
                    self.asr_server_receive = True
                    
        except asyncio.CancelledError:
            self.logger.bind(tag=TAG).info("Gemini响应处理任务已取消")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Gemini响应处理任务出错: {e}")
        finally:
            self.is_speaking = False
            self.asr_server_receive = True
    
    
    async def handle_audio_message(self, websocket, data):
        """处理音频消息"""
        try:
            # 获取音频数据
            audio_data = data.get("payload")
            if not audio_data:
                self.logger.bind(tag=TAG).warning("Received empty audio data")
                return

            # 检查当前是否允许接收音频
            if not self.asr_server_receive:
                self.logger.bind(tag=TAG).debug("服务端正在讲话，忽略音频输入")
                return

            # 添加到音频缓冲区
            self.client_audio_buffer.extend(audio_data)
            
            # 使用VAD检测语音活动
            have_voice, is_speaking_done = self.vad.detect(self.client_audio_buffer)
            
            current_time = time.time()
            
            # 处理语音检测结果
            if have_voice and not self.client_have_voice:
                # 检测到语音开始
                self.client_have_voice = True
                self.client_have_voice_last_time = current_time
                self.client_voice_stop = False
                self.asr_audio = []
                # 发送语音检测开始状态
                await websocket.send(json.dumps({
                    "type": "vad",
                    "state": "start",
                    "session_id": self.session_id
                }))
                
            if self.client_have_voice:
                # 当检测到语音时，添加到ASR音频缓冲区
                self.asr_audio.append(bytes(self.client_audio_buffer))
                self.client_audio_buffer = bytearray()
                
                # 检查语音是否结束
                if is_speaking_done or (current_time - self.client_have_voice_last_time > 2.0):
                    # 语音结束，进行ASR处理
                    self.client_voice_stop = True
                    self.client_have_voice = False
                    
                    # 发送语音检测结束状态
                    await websocket.send(json.dumps({
                        "type": "vad",
                        "state": "stop",
                        "session_id": self.session_id
                    }))
                    
                    # 如果有足够的音频数据，进行ASR识别
                    if len(self.asr_audio) > 0:
                        # 确保服务端不再接收音频，避免多次处理同一段语音
                        self.asr_server_receive = False
                        
                        # 合并所有音频数据
                        audio_data = b''.join(self.asr_audio)
                        self.asr_audio = []
                        
                        # 执行ASR识别
                        recognized_text = await self.asr.recognize(audio_data)
                        if recognized_text and len(recognized_text.strip()) > 0:
                            self.logger.bind(tag=TAG).info(f"ASR识别结果: {recognized_text}")
                            
                            # 发送ASR识别结果到客户端
                            await websocket.send(json.dumps({
                                "type": "asrResult",
                                "payload": recognized_text,
                                "session_id": self.session_id
                            }))
                            
                            # 重置客户端打断标志
                            self.client_abort = False
                            
                            # 使用LLM处理识别的文本
                            if self.use_function_call_mode:
                                await self.chat_with_function_calling(recognized_text)
                            else:
                                await self.chat(recognized_text)
                        else:
                            self.logger.bind(tag=TAG).warning("ASR识别为空，忽略")
                            self.asr_server_receive = True
                    else:
                        self.logger.bind(tag=TAG).warning("音频数据为空，忽略")
                        self.asr_server_receive = True
                        
                    # 重置VAD状态
                    self.reset_vad_states()
            
        except Exception as e:
            stack_trace = traceback.format_exc()
            self.logger.bind(tag=TAG).error(f"处理音频消息出错: {str(e)}\n{stack_trace}")
            self.asr_server_receive = True
            self.reset_vad_states()

    async def handle_stop_tts(self, websocket):
        """处理停止TTS请求"""
        self.logger.bind(tag=TAG).info("Received stop TTS request")
        self.should_stop_speaking = True
        self.client_abort = True
        if self.is_speaking:
            await self.send_tts_status(websocket, "stop")

    async def send_tts_status(self, websocket, state, text=None):
        """发送TTS状态消息"""
        message = {
            "type": "tts",
            "state": state,  # "start" 或 "stop"
            "session_id": self.session_id
        }
        if text is not None:
            message["text"] = text
            
        await websocket.send(json.dumps(message))

    async def _route_message(self, message):
        """消息路由"""
        if isinstance(message, str):
            await handleTextMessage(self, message)
        elif isinstance(message, bytes):
            await handleAudioMessage(self, message)

    def _initialize_components(self):
        """初始化组件"""
        self._initialize_models()

        """加载提示词"""
        self.prompt = self.config["prompt"]
        self.dialogue.put(Message(role="system", content=self.prompt))

        """加载记忆"""
        self._initialize_memory()
        """加载意图识别"""
        self._initialize_intent()
        """加载位置信息"""
        self.client_ip_info = get_ip_info(self.client_ip, self.logger)
        if self.client_ip_info is not None and "city" in self.client_ip_info:
            self.logger.bind(tag=TAG).info(f"Client ip info: {self.client_ip_info}")
            self.prompt = self.prompt + f"\nuser location:{self.client_ip_info}"

            self.dialogue.update_system_message(self.prompt)

    def _initialize_models(self):
        read_config_from_api = self.config.get("read_config_from_api", False)
        """如果是从配置文件获取，则进行二次实例化"""
        if not read_config_from_api:
            return
        """从接口获取差异化的配置进行二次实例化，非全量重新实例化"""
        try:
            private_config = get_private_config_from_api(
                self.config,
                self.headers.get("device-id", None),
                self.headers.get("client-id", None),
            )
            private_config["delete_audio"] = bool(self.config.get("delete_audio", True))
            self.logger.bind(tag=TAG).info(f"获取差异化配置成功: {private_config}")
        except DeviceNotFoundException as e:
            self.need_bind = True
            private_config = {}
        except DeviceBindException as e:
            self.need_bind = True
            self.bind_code = e.bind_code
            private_config = {}
        except Exception as e:
            self.need_bind = True
            self.logger.bind(tag=TAG).error(f"获取差异化配置失败: {e}")
            private_config = {}

        init_vad, init_asr, init_llm, init_tts, init_memory, init_intent = (
            False,
            False,
            False,
            False,
            False,
            False,
        )
        if private_config.get("VAD", None) is not None:
            init_vad = True
            self.config["VAD"] = private_config["VAD"]
            self.config["selected_module"]["VAD"] = private_config["selected_module"][
                "VAD"
            ]
        if private_config.get("ASR", None) is not None:
            init_asr = True
            self.config["ASR"] = private_config["ASR"]
            self.config["selected_module"]["ASR"] = private_config["selected_module"][
                "ASR"
            ]
        if private_config.get("LLM", None) is not None:
            init_llm = True
            self.config["LLM"] = private_config["LLM"]
            self.config["selected_module"]["LLM"] = private_config["selected_module"][
                "LLM"
            ]
        if private_config.get("TTS", None) is not None:
            init_tts = True
            self.config["TTS"] = private_config["TTS"]
            self.config["selected_module"]["TTS"] = private_config["selected_module"][
                "TTS"
            ]
        if private_config.get("Memory", None) is not None:
            init_memory = True
            self.config["Memory"] = private_config["Memory"]
            self.config["selected_module"]["Memory"] = private_config[
                "selected_module"
            ]["Memory"]
        if private_config.get("Intent", None) is not None:
            init_intent = True
            self.config["Intent"] = private_config["Intent"]
            self.config["selected_module"]["Intent"] = private_config[
                "selected_module"
            ]["Intent"]
        try:
            modules = initialize_modules(
                self.logger,
                private_config,
                init_vad,
                init_asr,
                init_llm,
                init_tts,
                init_memory,
                init_intent,
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"初始化组件失败: {e}")
            modules = {}
        if modules.get("vad", None) is not None:
            self.vad = modules["vad"]
        if modules.get("asr", None) is not None:
            self.asr = modules["asr"]
        if modules.get("tts", None) is not None:
            self.tts = modules["tts"]
        if modules.get("llm", None) is not None:
            self.llm = modules["llm"]
        if modules.get("intent", None) is not None:
            self.intent = modules["intent"]
        if modules.get("memory", None) is not None:
            self.memory = modules["memory"]
        if modules.get("prompt", None) is not None:
            self.change_system_prompt(modules["prompt"])

    def _initialize_memory(self):
        """初始化记忆模块"""
        device_id = self.headers.get("device-id", None)
        self.memory.init_memory(device_id, self.llm)

    def _initialize_intent(self):
        if (
            self.config["Intent"][self.config["selected_module"]["Intent"]]["type"]
            == "function_call"
        ):
            self.use_function_call_mode = True
        """初始化意图识别模块"""
        # 获取意图识别配置
        intent_config = self.config["Intent"]
        intent_type = self.config["Intent"][self.config["selected_module"]["Intent"]][
            "type"
        ]

        # 如果使用 nointent，直接返回
        if intent_type == "nointent":
            return
        # 使用 intent_llm 模式
        elif intent_type == "intent_llm":
            intent_llm_name = intent_config[self.config["selected_module"]["Intent"]][
                "llm"
            ]

            if intent_llm_name and intent_llm_name in self.config["LLM"]:
                # 如果配置了专用LLM，则创建独立的LLM实例
                from core.utils import llm as llm_utils

                intent_llm_config = self.config["LLM"][intent_llm_name]
                intent_llm_type = intent_llm_config.get("type", intent_llm_name)
                intent_llm = llm_utils.create_instance(
                    intent_llm_type, intent_llm_config
                )
                self.logger.bind(tag=TAG).info(
                    f"为意图识别创建了专用LLM: {intent_llm_name}, 类型: {intent_llm_type}"
                )
                self.intent.set_llm(intent_llm)
            else:
                # 否则使用主LLM
                self.intent.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("使用主LLM作为意图识别模型")

        """加载插件"""
        self.func_handler = FunctionHandler(self)
        self.mcp_manager = MCPManager(self)

        """加载MCP工具"""
        asyncio.run_coroutine_threadsafe(
            self.mcp_manager.initialize_servers(), self.loop
        )

    def change_system_prompt(self, prompt):
        self.prompt = prompt
        # 更新系统prompt至上下文
        self.dialogue.update_system_message(self.prompt)

    async def chat(self, query):
        """使用流式TTS进行对话"""
        self.dialogue.put(Message(role="user", content=query))

        response_message = []
        processed_chars = 0  # 跟踪已处理的字符位置
        try:
            # 使用带记忆的对话
            memory_str = await self.memory.query_memory(query)
            self.logger.bind(tag=TAG).debug(f"记忆内容: {memory_str}")
            llm_responses = self.llm.response(
                self.session_id, self.dialogue.get_llm_dialogue_with_memory(memory_str)
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM 处理出错 {query}: {e}")
            return None

        self.llm_finish_task = False
        text_index = 0
        for content in llm_responses:
            response_message.append(content)
            if self.client_abort:
                break

            # 合并当前全部文本并处理未分割部分
            full_text = "".join(response_message)
            current_text = full_text[processed_chars:]  # 从未处理的位置开始

            # 查找最后一个有效标点
            punctuations = ("。", "？", "！", "；", "：")
            last_punct_pos = -1
            for punct in punctuations:
                pos = current_text.rfind(punct)
                if pos > last_punct_pos:
                    last_punct_pos = pos

            # 找到分割点则处理
            if last_punct_pos != -1:
                segment_text_raw = current_text[: last_punct_pos + 1]
                segment_text = get_string_no_punctuation_or_emoji(segment_text_raw)
                if segment_text:
                    text_index += 1
                    self.recode_first_last_text(segment_text, text_index)
                    
                    # 使用流式TTS处理
                    await self.stream_tts_response(self.websocket, segment_text)
                    processed_chars += len(segment_text_raw)  # 更新已处理字符位置

        # 处理最后剩余的文本
        full_text = "".join(response_message)
        remaining_text = full_text[processed_chars:]
        if remaining_text:
            segment_text = get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                text_index += 1
                self.recode_first_last_text(segment_text, text_index)
                await self.stream_tts_response(self.websocket, segment_text)

        self.llm_finish_task = True
        self.dialogue.put(Message(role="assistant", content="".join(response_message)))
        self.logger.bind(tag=TAG).debug(
            json.dumps(self.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False)
        )
        return True

    async def stream_tts_response(self, websocket, text):
        """流式发送TTS音频"""
        if not text:
            return
            
        self.should_stop_speaking = False
        self.is_speaking = True
        self.asr_server_receive = False  # 发言时不接收音频输入
        
        try:
            # 发送TTS开始状态
            await self.send_tts_status(websocket, "start", text)
            
            try:
                # 尝试使用流式生成 (如果TTS引擎支持)
                if hasattr(self.tts, 'generate_streaming'):
                    # 直接从TTS引擎获取流式音频
                    audio_stream = self.tts.generate_streaming(text)
                    
                    # 发送音频数据
                    async for audio_chunk in audio_stream:
                        # 检查是否应该停止
                        if self.should_stop_speaking or websocket.closed:
                            break
                            
                        # 发送二进制音频数据
                        await websocket.send(audio_chunk)
                        
                        # 控制发送速率
                        delay = self.config.get("tts_stream_delay", 0.01)
                        await asyncio.sleep(delay)
                else:
                    # TTS引擎不支持流式生成，回退到传统方法
                    self.logger.bind(tag=TAG).info("TTS引擎不支持流式生成，回退到传统方法")
                    
                    # 使用传统方法生成TTS文件
                    tts_file = self.tts.to_tts(text)
                    
                    if tts_file and os.path.exists(tts_file):
                        try:
                            # 转换为opus格式并发送
                            opus_datas, _ = self.tts.audio_to_opus_data(tts_file)
                            
                            for data in opus_datas:
                                if self.should_stop_speaking or websocket.closed:
                                    break
                                await websocket.send(data)
                                await asyncio.sleep(0.01)
                                
                            # 完成后清理文件
                            if self.tts.delete_audio_file:
                                os.remove(tts_file)
                        except Exception as inner_e:
                            self.logger.bind(tag=TAG).error(f"处理TTS文件时出错: {str(inner_e)}")
                    else:
                        self.logger.bind(tag=TAG).error(f"TTS文件生成失败: {tts_file}")
                    
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"TTS处理出错: {str(e)}")
                # 出错时也尝试发送停止状态
                if not websocket.closed:
                    await self.send_tts_status(websocket, "stop")
                    
            # 发送TTS结束状态
            if not websocket.closed:
                await self.send_tts_status(websocket, "stop")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"流式TTS处理失败: {str(e)}")
            if not websocket.closed:
                await self.send_tts_status(websocket, "stop")
        finally:
            self.is_speaking = False
            self.asr_server_receive = True  # 恢复音频接收

    async def chat_with_function_calling(self, query, tool_call=False):
        self.logger.bind(tag=TAG).debug(f"Chat with function calling start: {query}")
        """Chat with function calling for intent detection using streaming"""

        if not tool_call:
            self.dialogue.put(Message(role="user", content=query))

        # Define intent functions
        functions = None
        if hasattr(self, "func_handler"):
            functions = self.func_handler.get_functions()
        response_message = []
        processed_chars = 0  # 跟踪已处理的字符位置

        try:
            start_time = time.time()

            # 使用带记忆的对话
            memory_str = await self.memory.query_memory(query)

            # self.logger.bind(tag=TAG).info(f"对话记录: {self.dialogue.get_llm_dialogue_with_memory(memory_str)}")

            # 使用支持functions的streaming接口
            llm_responses = self.llm.response_with_functions(
                self.session_id,
                self.dialogue.get_llm_dialogue_with_memory(memory_str),
                functions=functions,
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM 处理出错 {query}: {e}")
            return None

        self.llm_finish_task = False
        text_index = 0

        # 处理流式响应
        tool_call_flag = False
        function_name = None
        function_id = None
        function_arguments = ""
        content_arguments = ""
        for response in llm_responses:
            content, tools_call = response
            if "content" in response:
                content = response["content"]
                tools_call = None
            if content is not None and len(content) > 0:
                if len(response_message) <= 0 and (
                    content == "```" or "<tool_call>" in content
                ):
                    tool_call_flag = True

            if tools_call is not None:
                tool_call_flag = True
                if tools_call[0].id is not None:
                    function_id = tools_call[0].id
                if tools_call[0].function.name is not None:
                    function_name = tools_call[0].function.name
                if tools_call[0].function.arguments is not None:
                    function_arguments += tools_call[0].function.arguments

            if content is not None and len(content) > 0:
                if tool_call_flag:
                    content_arguments += content
                else:
                    response_message.append(content)

                    if self.client_abort:
                        break

                    end_time = time.time()
                    # self.logger.bind(tag=TAG).debug(f"大模型返回时间: {end_time - start_time} 秒, 生成token={content}")

                    # 处理文本分段和TTS逻辑
                    # 合并当前全部文本并处理未分割部分
                    full_text = "".join(response_message)
                    current_text = full_text[processed_chars:]  # 从未处理的位置开始

                    # 查找最后一个有效标点
                    punctuations = ("。", "？", "！", "；", "：")
                    last_punct_pos = -1
                    for punct in punctuations:
                        pos = current_text.rfind(punct)
                        if pos > last_punct_pos:
                            last_punct_pos = pos

                    # 找到分割点则处理
                    if last_punct_pos != -1:
                        segment_text_raw = current_text[: last_punct_pos + 1]
                        segment_text = get_string_no_punctuation_or_emoji(
                            segment_text_raw
                        )
                        if segment_text:
                            text_index += 1
                            self.recode_first_last_text(segment_text, text_index)
                            # 使用流式TTS处理
                            await self.stream_tts_response(self.websocket, segment_text)
                            processed_chars += len(segment_text_raw)  # 更新已处理字符位置

        # 处理function call
        if tool_call_flag:
            bHasError = False
            if function_id is None:
                a = extract_json_from_string(content_arguments)
                if a is not None:
                    try:
                        content_arguments_json = json.loads(a)
                        function_name = content_arguments_json["name"]
                        function_arguments = json.dumps(
                            content_arguments_json["arguments"], ensure_ascii=False
                        )
                        function_id = str(uuid.uuid4().hex)
                    except Exception as e:
                        bHasError = True
                        response_message.append(a)
                else:
                    bHasError = True
                    response_message.append(content_arguments)
                if bHasError:
                    self.logger.bind(tag=TAG).error(
                        f"function call error: {content_arguments}"
                    )
                else:
                    function_arguments = json.loads(function_arguments)
            if not bHasError:
                self.logger.bind(tag=TAG).info(
                    f"function_name={function_name}, function_id={function_id}, function_arguments={function_arguments}"
                )
                function_call_data = {
                    "name": function_name,
                    "id": function_id,
                    "arguments": function_arguments,
                }

                # 处理MCP工具调用
                if self.mcp_manager.is_mcp_tool(function_name):
                    result = await self._handle_mcp_tool_call(function_call_data)
                else:
                    # 处理系统函数
                    result = self.func_handler.handle_llm_function_call(
                        self, function_call_data
                    )
                await self._handle_function_result(result, function_call_data, text_index + 1)

        # 处理最后剩余的文本
        full_text = "".join(response_message)
        remaining_text = full_text[processed_chars:]
        if remaining_text:
            segment_text = get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                text_index += 1
                self.recode_first_last_text(segment_text, text_index)
                await self.stream_tts_response(self.websocket, segment_text)

        # 存储对话内容
        if len(response_message) > 0:
            self.dialogue.put(
                Message(role="assistant", content="".join(response_message))
            )

        self.llm_finish_task = True
        self.logger.bind(tag=TAG).debug(
            json.dumps(self.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False)
        )

        return True

    async def _handle_mcp_tool_call(self, function_call_data):
        function_arguments = function_call_data["arguments"]
        function_name = function_call_data["name"]
        try:
            args_dict = function_arguments
            if isinstance(function_arguments, str):
                try:
                    args_dict = json.loads(function_arguments)
                except json.JSONDecodeError:
                    self.logger.bind(tag=TAG).error(
                        f"无法解析 function_arguments: {function_arguments}"
                    )
                    return ActionResponse(
                        action=Action.REQLLM, result="参数解析失败", response=""
                    )

            tool_result = await self.mcp_manager.execute_tool(function_name, args_dict)
            
            # meta=None content=[TextContent(type='text', text='北京当前天气:\n温度: 21°C\n天气: 晴\n湿度: 6%\n风向: 西北 风\n风力等级: 5级', annotations=None)] isError=False
            content_text = ""
            if tool_result is not None and tool_result.content is not None:
                for content in tool_result.content:
                    content_type = content.type
                    if content_type == "text":
                        content_text = content.text
                    elif content_type == "image":
                        pass

            if len(content_text) > 0:
                return ActionResponse(
                    action=Action.REQLLM, result=content_text, response=""
                )

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"MCP工具调用错误: {e}")
            return ActionResponse(
                action=Action.REQLLM, result="工具调用出错", response=""
            )

        return ActionResponse(action=Action.REQLLM, result="工具调用出错", response="")

    async def _handle_function_result(self, result, function_call_data, text_index):
        if result.action == Action.RESPONSE:  # 直接回复前端
            text = result.response
            self.recode_first_last_text(text, text_index)
            await self.stream_tts_response(self.websocket, text)
            self.dialogue.put(Message(role="assistant", content=text))
        elif result.action == Action.REQLLM:  # 调用函数后再请求llm生成回复
            text = result.result
            if text is not None and len(text) > 0:
                function_id = function_call_data["id"]
                function_name = function_call_data["name"]
                function_arguments = function_call_data["arguments"]
                self.dialogue.put(
                    Message(
                        role="assistant",
                        tool_calls=[
                            {
                                "id": function_id,
                                "function": {
                                    "arguments": function_arguments,
                                    "name": function_name,
                                },
                                "type": "function",
                                "index": 0,
                            }
                        ],
                    )
                )

                self.dialogue.put(
                    Message(role="tool", tool_call_id=function_id, content=text)
                )
                await self.chat_with_function_calling(text, tool_call=True)
        elif result.action == Action.NOTFOUND:
            text = result.result
            self.recode_first_last_text(text, text_index)
            await self.stream_tts_response(self.websocket, text)
            self.dialogue.put(Message(role="assistant", content=text))
        else:
            text = result.result
            self.recode_first_last_text(text, text_index)
            await self.stream_tts_response(self.websocket, text)
            self.dialogue.put(Message(role="assistant", content=text))

    def _tts_priority_thread(self):
        while not self.stop_event.is_set():
            text = None
            try:
                try:
                    future = self.tts_queue.get(timeout=1)
                except queue.Empty:
                    if self.stop_event.is_set():
                        break
                    continue
                if future is None:
                    continue
                text = None
                opus_datas, text_index, tts_file = [], 0, None
                try:
                    self.logger.bind(tag=TAG).debug("正在处理TTS任务...")
                    tts_timeout = int(self.config.get("tts_timeout", 10))
                    tts_file, text, text_index = future.result(timeout=tts_timeout)
                    if text is None or len(text) <= 0:
                        self.logger.bind(tag=TAG).error(
                            f"TTS出错：{text_index}: tts text is empty"
                        )
                    elif tts_file is None:
                        self.logger.bind(tag=TAG).error(
                            f"TTS出错： file is empty: {text_index}: {text}"
                        )
                    else:
                        self.logger.bind(tag=TAG).debug(
                            f"TTS生成：文件路径: {tts_file}"
                        )
                        if os.path.exists(tts_file):
                            opus_datas, duration = self.tts.audio_to_opus_data(tts_file)
                        else:
                            self.logger.bind(tag=TAG).error(
                                f"TTS出错：文件不存在{tts_file}"
                            )
                except TimeoutError:
                    self.logger.bind(tag=TAG).error("TTS超时")
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"TTS出错: {e}")
                if not self.client_abort:
                    # 如果没有中途打断就发送语音
                    self.audio_play_queue.put((opus_datas, text, text_index))
                if (
                    self.tts.delete_audio_file
                    and tts_file is not None
                    and os.path.exists(tts_file)
                ):
                    os.remove(tts_file)
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"TTS任务处理错误: {e}")
                self.clearSpeakStatus()
                asyncio.run_coroutine_threadsafe(
                    self.send_tts_status(self.websocket, "stop"),
                    self.loop,
                )
                self.logger.bind(tag=TAG).error(
                    f"tts_priority priority_thread: {text} {e}"
                )

    def _audio_play_priority_thread(self):
        while not self.stop_event.is_set():
            text = None
            try:
                try:
                    opus_datas, text, text_index = self.audio_play_queue.get(timeout=1)
                except queue.Empty:
                    if self.stop_event.is_set():
                        break
                    continue
                
                # 已改用流式TTS，此处为了兼容旧代码保留
                asyncio.run_coroutine_threadsafe(
                    self.send_tts_status(self.websocket, "start", text),
                    self.loop
                ).result()
                
                for data in opus_datas:
                    if self.client_abort or self.should_stop_speaking:
                        break
                    asyncio.run_coroutine_threadsafe(
                        self.websocket.send(data),
                        self.loop
                    ).result()
                    time.sleep(0.01)
                    
                asyncio.run_coroutine_threadsafe(
                    self.send_tts_status(self.websocket, "stop"),
                    self.loop
                ).result()
                
            except Exception as e:
                self.logger.bind(tag=TAG).error(
                    f"audio_play_priority priority_thread: {text} {e}"
                )

    def speak_and_play(self, text, text_index=0):
        if text is None or len(text) <= 0:
            self.logger.bind(tag=TAG).info(f"无需tts转换，query为空，{text}")
            return None, text, text_index
        tts_file = self.tts.to_tts(text)
        if tts_file is None:
            self.logger.bind(tag=TAG).error(f"tts转换失败，{text}")
            return None, text, text_index
        self.logger.bind(tag=TAG).debug(f"TTS 文件生成完毕: {tts_file}")
        return tts_file, text, text_index

    def clearSpeakStatus(self):
        self.logger.bind(tag=TAG).debug(f"清除服务端讲话状态")
        self.asr_server_receive = True
        self.tts_last_text_index = -1
        self.tts_first_text_index = -1
        self.is_speaking = False
        self.should_stop_speaking = False

    def recode_first_last_text(self, text, text_index=0):
        if self.tts_first_text_index == -1:
            self.logger.bind(tag=TAG).info(f"大模型说出第一句话: {text}")
            self.tts_first_text_index = text_index
        self.tts_last_text_index = text_index

    async def close(self, ws=None):
        """资源清理方法"""
        # 清理MCP资源
        if hasattr(self, "mcp_manager") and self.mcp_manager:
            await self.mcp_manager.cleanup_all()

        # 触发停止事件并清理资源
        if self.stop_event:
            self.stop_event.set()

        # 立即关闭线程池
        if self.executor:
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor = None

        # 清空任务队列
        self._clear_queues()

        if ws:
            await ws.close()
        elif self.websocket:
            await self.websocket.close()
        self.logger.bind(tag=TAG).info("连接资源已释放")

    def _clear_queues(self):
        # 清空所有任务队列
        for q in [self.tts_queue, self.audio_play_queue]:
            if not q:
                continue
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    continue
            q.queue.clear()
            # 添加毒丸信号到队列，确保线程退出
            # q.queue.put(None)

    def reset_vad_states(self):
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_have_voice_last_time = 0
        self.client_voice_stop = False
        self.logger.bind(tag=TAG).debug("VAD states reset.")

    def chat_and_close(self, text):
        """Chat with the user and then close the connection"""
        try:
            # Use the existing chat method
            self.chat(text)

            # After chat is complete, close the connection
            self.close_after_chat = True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Chat and close error: {str(e)}")