import os
import uuid
import asyncio
import io
import edge_tts
import numpy as np
from pydub import AudioSegment
from core.utils.tts import MarkdownCleaner
from core.providers.tts.base import TTSProviderBase, logger, TAG

class EdgeTTSProvider(TTSProviderBase):
    def __init__(self, config):
        super().__init__(config, config.get("delete_audio_file", True))
        self.voice = config.get("voice", "zh-CN-XiaoxiaoNeural")
        self.rate = config.get("rate", "+0%")
        self.volume = config.get("volume", "+0%")
        
    def generate_filename(self):
        """生成临时文件名"""
        if not os.path.exists(self.output_file):
            os.makedirs(self.output_file)
        return os.path.join(self.output_file, f"{uuid.uuid4()}.mp3")
        
    async def text_to_speak(self, text, output_file):
        """将文本转换为语音并保存到文件"""
        try:
            communicate = edge_tts.Communicate(
                text, self.voice, rate=self.rate, volume=self.volume
            )
            await communicate.save(output_file)
        except Exception as e:
            logger.bind(tag=TAG).error(f"Edge TTS转换失败: {e}")
            raise
            
    async def generate_streaming(self, text, **kwargs):
        """生成流式音频数据"""
        # 清理Markdown格式
        text = MarkdownCleaner.clean_markdown(text)
        
        try:
            # 创建Edge TTS通信对象
            communicate = edge_tts.Communicate(
                text, self.voice, rate=self.rate, volume=self.volume
            )
            
            # 初始化缓冲
            audio_buffer = io.BytesIO()
            accumulated_bytes = 0
            
            # 流式获取音频数据
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    # 收集音频数据块
                    audio_buffer.write(chunk["data"])
                    accumulated_bytes += len(chunk["data"])
                    
                    # 积累一定量的数据再编码为opus并发送
                    # 每积累约200ms的音频数据处理一次
                    if accumulated_bytes >= 4000:  # ~200ms of MP3 data
                        # 获取当前缓冲区的数据
                        audio_data = audio_buffer.getvalue()
                        audio_buffer = io.BytesIO()  # 重置缓冲区
                        accumulated_bytes = 0
                        
                        try:
                            # 从MP3转换为PCM
                            audio_segment = AudioSegment.from_mp3(io.BytesIO(audio_data))
                            # 处理音频格式
                            audio_segment = audio_segment.set_channels(1).set_frame_rate(16000).set_sample_width(2)
                            pcm_data = audio_segment.raw_data
                            
                            # 对PCM数据编码为opus
                            opus_datas, _ = self.audio_to_opus_data(io.BytesIO(pcm_data))
                            
                            # 返回每个opus块
                            for opus_chunk in opus_datas:
                                yield opus_chunk
                        except Exception as e:
                            logger.bind(tag=TAG).error(f"处理音频块失败: {e}")
                            continue
                            
            # 处理剩余的音频数据
            if accumulated_bytes > 0:
                audio_data = audio_buffer.getvalue()
                
                try:
                    # 从MP3转换为PCM
                    audio_segment = AudioSegment.from_mp3(io.BytesIO(audio_data))
                    # 处理音频格式
                    audio_segment = audio_segment.set_channels(1).set_frame_rate(16000).set_sample_width(2)
                    pcm_data = audio_segment.raw_data
                    
                    # 对PCM数据编码为opus
                    opus_datas, _ = self.audio_to_opus_data(io.BytesIO(pcm_data))
                    
                    # 返回每个opus块
                    for opus_chunk in opus_datas:
                        yield opus_chunk
                except Exception as e:
                    logger.bind(tag=TAG).error(f"处理最终音频块失败: {e}")
                    
        except Exception as e:
            logger.bind(tag=TAG).error(f"Edge TTS 流式转换失败: {e}")
            raise
    
    def audio_to_opus_data(self, audio_file_path_or_io):
        """从文件或IO对象转换为Opus编码
        
        参数:
            audio_file_path_or_io: 文件路径或包含音频数据的IO对象
        """
        try:
            # 检查是否是IO对象
            if hasattr(audio_file_path_or_io, 'read'):
                # 从IO对象读取音频
                audio = AudioSegment.from_file(audio_file_path_or_io, format="raw", 
                                               frame_rate=16000, channels=1, sample_width=2)
            else:
                # 从文件读取音频
                file_type = os.path.splitext(audio_file_path_or_io)[1]
                if file_type:
                    file_type = file_type.lstrip('.')
                audio = AudioSegment.from_file(audio_file_path_or_io, format=file_type, parameters=["-nostdin"])
                
                # 转换为单声道/16kHz采样率/16位小端编码
                audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
                
            # 音频时长(秒)
            duration = len(audio) / 1000.0
            
            # 获取原始PCM数据
            raw_data = audio.raw_data
            
            # 使用父类的方法进行Opus编码
            return super().audio_to_opus_data(audio_file_path_or_io)
            
        except Exception as e:
            logger.bind(tag=TAG).error(f"音频转换为Opus编码失败: {e}")
            return [], 0