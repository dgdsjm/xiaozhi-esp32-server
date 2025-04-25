from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
class AuthenticationError(Exception):
    """认证异常"""
    pass
class AuthMiddleware:
    def __init__(self, config):
        self.config = config
        self.auth_config = config["server"].get("auth", {})
        # 构建token查找表
        self.tokens = {
            item["token"]: item["name"]
            for item in self.auth_config.get("tokens", [])
        }
        # 设备白名单
        self.allowed_devices = set(
            self.auth_config.get("allowed_devices", [])
        )
    async def authenticate(self, headers):
        """验证连接请求"""
        # 检查是否启用认证
        if not self.auth_config.get("enabled", False):
            return True
        # 检查设备是否在白名单中
        device_id = headers.get("device-id", "")
        if self.allowed_devices and device_id in self.allowed_devices:
            return True
        # 验证Authorization header
        auth_header = headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.bind(tag=TAG).error("Missing or invalid Authorization header")
            raise AuthenticationError("Missing or invalid Authorization header")
        token = auth_header.split(" ")[1]
        if token not in self.tokens:
            logger.bind(tag=TAG).error(f"Invalid token: {token}")
            raise AuthenticationError("Invalid token")
        logger.bind(tag=TAG).info(f"Authentication successful - Device: {device_id}, Token: {self.tokens[token]}")
        return True
    def get_token_name(self, token):
        """获取token对应的设备名称"""
        return self.tokens.get(token)
import os
import copy
import json
import uuid
import time
import queue
import asyncio
import traceback
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
from core.handle.sendAudioHandle import sendAudioMessage
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
                    await self._route_message(message)
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
    def chat(self, query):
        self.dialogue.put(Message(role="user", content=query))
        response_message = []
        processed_chars = 0  # 跟踪已处理的字符位置
        try:
            # 使用带记忆的对话
            future = asyncio.run_coroutine_threadsafe(
                self.memory.query_memory(query), self.loop
            )
            memory_str = future.result()
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
                    # 强制设置空字符，测试TTS出错返回语音的健壮性
                    # if text_index % 2 == 0:
                    #     segment_text = " "
                    text_index += 1
                    self.recode_first_last_text(segment_text, text_index)
                    future = self.executor.submit(
                        self.speak_and_play, segment_text, text_index
                    )
                    self.tts_queue.put(future)
                    processed_chars += len(segment_text_raw)  # 更新已处理字符位置
        # 处理最后剩余的文本
        full_text = "".join(response_message)
        remaining_text = full_text[processed_chars:]
        if remaining_text:
            segment_text = get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                text_index += 1
                self.recode_first_last_text(segment_text, text_index)
                future = self.executor.submit(
                    self.speak_and_play, segment_text, text_index
                )
                self.tts_queue.put(future)
        self.llm_finish_task = True
        self.dialogue.put(Message(role="assistant", content="".join(response_message)))
        self.logger.bind(tag=TAG).debug(
            json.dumps(self.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False)
        )
        return True
    def chat_with_function_calling(self, query, tool_call=False):
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
            future = asyncio.run_coroutine_threadsafe(
                self.memory.query_memory(query), self.loop
            )
            memory_str = future.result()
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
                            future = self.executor.submit(
                                self.speak_and_play, segment_text, text_index
                            )
                            self.tts_queue.put(future)
                            # 更新已处理字符位置
                            processed_chars += len(segment_text_raw)
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
                    result = self._handle_mcp_tool_call(function_call_data)
                else:
                    # 处理系统函数
                    result = self.func_handler.handle_llm_function_call(
                        self, function_call_data
                    )
                self._handle_function_result(result, function_call_data, text_index + 1)
        # 处理最后剩余的文本
        full_text = "".join(response_message)
        remaining_text = full_text[processed_chars:]
        if remaining_text:
            segment_text = get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                text_index += 1
                self.recode_first_last_text(segment_text, text_index)
                future = self.executor.submit(
                    self.speak_and_play, segment_text, text_index
                )
                self.tts_queue.put(future)
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
    def _handle_mcp_tool_call(self, function_call_data):
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
            tool_result = asyncio.run_coroutine_threadsafe(
                self.mcp_manager.execute_tool(function_name, args_dict), self.loop
            ).result()
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
    def _handle_function_result(self, result, function_call_data, text_index):
        if result.action == Action.RESPONSE:  # 直接回复前端
            text = result.response
            self.recode_first_last_text(text, text_index)
            future = self.executor.submit(self.speak_and_play, text, text_index)
            self.tts_queue.put(future)
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
                self.chat_with_function_calling(text, tool_call=True)
        elif result.action == Action.NOTFOUND:
            text = result.result
            self.recode_first_last_text(text, text_index)
            future = self.executor.submit(self.speak_and_play, text, text_index)
            self.tts_queue.put(future)
            self.dialogue.put(Message(role="assistant", content=text))
        else:
            text = result.result
            self.recode_first_last_text(text, text_index)
            future = self.executor.submit(self.speak_and_play, text, text_index)
            self.tts_queue.put(future)
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
                    self.websocket.send(
                        json.dumps(
                            {
                                "type": "tts",
                                "state": "stop",
                                "session_id": self.session_id,
                            }
                        )
                    ),
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
                future = asyncio.run_coroutine_threadsafe(
                    sendAudioMessage(self, opus_datas, text, text_index), self.loop
                )
                future.result()
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
import json
import queue
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
async def handleAbortMessage(conn):
    logger.bind(tag=TAG).info("Abort message received")
    # 设置成打断状态，会自动打断llm、tts任务
    conn.client_abort = True
    # 打断客户端说话状态
    await conn.websocket.send(json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id}))
    conn.clearSpeakStatus()
    logger.bind(tag=TAG).info("Abort message received-end")
from config.logger import setup_logging
import json
from plugins_func.register import FunctionRegistry, ActionResponse, Action, ToolType
from plugins_func.functions.hass_init import append_devices_to_prompt
TAG = __name__
logger = setup_logging()
class FunctionHandler:
    def __init__(self, conn):
        self.conn = conn
        self.config = conn.config
        self.function_registry = FunctionRegistry()
        self.register_nessary_functions()
        self.register_config_functions()
        self.functions_desc = self.function_registry.get_all_function_desc()
        func_names = self.current_support_functions()
        self.modify_plugin_loader_des(func_names)
        self.finish_init = True
    def modify_plugin_loader_des(self, func_names):
        if "plugin_loader" not in func_names:
            return
        # 可编辑的列表中去掉plugin_loader
        surport_plugins = [func for func in func_names if func != "plugin_loader"]
        func_names = ",".join(surport_plugins)
        for function_desc in self.functions_desc:
            if function_desc["function"]["name"] == "plugin_loader":
                function_desc["function"]["description"] = function_desc["function"][
                    "description"
                ].replace("[plugins]", func_names)
                break
    def upload_functions_desc(self):
        self.functions_desc = self.function_registry.get_all_function_desc()
    def current_support_functions(self):
        func_names = []
        for func in self.functions_desc:
            func_names.append(func["function"]["name"])
        # 打印当前支持的函数列表
        logger.bind(tag=TAG).info(f"当前支持的函数列表: {func_names}")
        return func_names
    def get_functions(self):
        """获取功能调用配置"""
        return self.functions_desc
    def register_nessary_functions(self):
        """注册必要的函数"""
        self.function_registry.register_function("handle_exit_intent")
        self.function_registry.register_function("plugin_loader")
        self.function_registry.register_function("get_time")
        self.function_registry.register_function("get_lunar")
        self.function_registry.register_function("handle_device")
    def register_config_functions(self):
        """注册配置中的函数,可以不同客户端使用不同的配置"""
        for func in self.config["Intent"][self.config["selected_module"]["Intent"]].get(
            "functions", []
        ):
            self.function_registry.register_function(func)
        """home assistant需要初始化提示词"""
        append_devices_to_prompt(self.conn)
    def get_function(self, name):
        return self.function_registry.get_function(name)
    def handle_llm_function_call(self, conn, function_call_data):
        try:
            function_name = function_call_data["name"]
            funcItem = self.get_function(function_name)
            if not funcItem:
                return ActionResponse(
                    action=Action.NOTFOUND, result="没有找到对应的函数", response=""
                )
            func = funcItem.func
            arguments = function_call_data["arguments"]
            arguments = json.loads(arguments) if arguments else {}
            logger.bind(tag=TAG).info(f"调用函数: {function_name}, 参数: {arguments}")
            if (
                funcItem.type == ToolType.SYSTEM_CTL
                or funcItem.type == ToolType.IOT_CTL
            ):
                return func(conn, **arguments)
            elif funcItem.type == ToolType.WAIT:
                return func(**arguments)
            elif funcItem.type == ToolType.CHANGE_SYS_PROMPT:
                return func(conn, **arguments)
            else:
                return ActionResponse(
                    action=Action.NOTFOUND, result="没有找到对应的函数", response=""
                )
        except Exception as e:
            logger.bind(tag=TAG).error(f"处理function call错误: {e}")
        return None
import json
from config.logger import setup_logging
from core.handle.sendAudioHandle import send_stt_message
from core.utils.util import remove_punctuation_and_length
import shutil
import asyncio
import os
import random
import time
TAG = __name__
logger = setup_logging()
WAKEUP_CONFIG = {
    "dir": "config/assets/",
    "file_name": "wakeup_words",
    "create_time": time.time(),
    "refresh_time": 10,
    "words": ["你好小智", "你好啊小智", "小智你好", "小智"],
    "text": "",
}
async def handleHelloMessage(conn):
    await conn.websocket.send(json.dumps(conn.welcome_msg))
async def checkWakeupWords(conn, text):
    enable_wakeup_words_response_cache = conn.config[
        "enable_wakeup_words_response_cache"
    ]
    """是否开启唤醒词加速"""
    if not enable_wakeup_words_response_cache:
        return False
    """检查是否是唤醒词"""
    _, text = remove_punctuation_and_length(text)
    if text in conn.config.get("wakeup_words"):
        await send_stt_message(conn, text)
        conn.tts_first_text_index = 0
        conn.tts_last_text_index = 0
        conn.llm_finish_task = True
        file = getWakeupWordFile(WAKEUP_CONFIG["file_name"])
        if file is None:
            asyncio.create_task(wakeupWordsResponse(conn))
            return False
        opus_packets, duration = conn.tts.audio_to_opus_data(file)
        text_hello = WAKEUP_CONFIG["text"]
        if not text_hello:
            text_hello = text
        conn.audio_play_queue.put((opus_packets, text_hello, 0))
        if time.time() - WAKEUP_CONFIG["create_time"] > WAKEUP_CONFIG["refresh_time"]:
            asyncio.create_task(wakeupWordsResponse(conn))
        return True
    return False
def getWakeupWordFile(file_name):
    for file in os.listdir(WAKEUP_CONFIG["dir"]):
        if file.startswith("my_" + file_name):
            """避免缓存文件是一个空文件"""
            if os.stat(f"config/assets/{file}").st_size > (15 * 1024):
                return f"config/assets/{file}"
    """查找config/assets/目录下名称为wakeup_words的文件"""
    for file in os.listdir(WAKEUP_CONFIG["dir"]):
        if file.startswith(file_name):
            return f"config/assets/{file}"
    return None
async def wakeupWordsResponse(conn):
    wait_max_time = 5
    while conn.llm is None or not conn.llm.response_no_stream:
        await asyncio.sleep(1)
        wait_max_time -= 1
        if wait_max_time <= 0:
            logger.bind(tag=TAG).error("连接对象没有llm")
            return
    """唤醒词响应"""
    wakeup_word = random.choice(WAKEUP_CONFIG["words"])
    result = conn.llm.response_no_stream(conn.config["prompt"], wakeup_word)
    tts_file = await asyncio.to_thread(conn.tts.to_tts, result)
    if tts_file is not None and os.path.exists(tts_file):
        file_type = os.path.splitext(tts_file)[1]
        if file_type:
            file_type = file_type.lstrip(".")
        old_file = getWakeupWordFile("my_" + WAKEUP_CONFIG["file_name"])
        if old_file is not None:
            os.remove(old_file)
        """将文件挪到"wakeup_words.mp3"""
        shutil.move(
            tts_file,
            WAKEUP_CONFIG["dir"] + "my_" + WAKEUP_CONFIG["file_name"] + "." + file_type,
        )
        WAKEUP_CONFIG["create_time"] = time.time()
        WAKEUP_CONFIG["text"] = result
from config.logger import setup_logging
import json
import uuid
from core.handle.sendAudioHandle import send_stt_message
from core.handle.helloHandle import checkWakeupWords
from core.utils.util import remove_punctuation_and_length
from core.utils.dialogue import Message
from loguru import logger
TAG = __name__
logger = setup_logging()
async def handle_user_intent(conn, text):
    # 检查是否有明确的退出命令
    if await check_direct_exit(conn, text):
        return True
    # 检查是否是唤醒词
    if await checkWakeupWords(conn, text):
        return True
    if conn.use_function_call_mode:
        # 使用支持function calling的聊天方法,不再进行意图分析
        return False
    # 使用LLM进行意图分析
    intent_result = await analyze_intent_with_llm(conn, text)
    if not intent_result:
        return False
    # 处理各种意图
    return await process_intent_result(conn, intent_result, text)
async def check_direct_exit(conn, text):
    """检查是否有明确的退出命令"""
    _, text = remove_punctuation_and_length(text)
    cmd_exit = conn.cmd_exit
    for cmd in cmd_exit:
        if text == cmd:
            logger.bind(tag=TAG).info(f"识别到明确的退出命令: {text}")
            await send_stt_message(conn, text)
            await conn.close()
            return True
    return False
async def analyze_intent_with_llm(conn, text):
    """使用LLM分析用户意图"""
    if not hasattr(conn, "intent") or not conn.intent:
        logger.bind(tag=TAG).warning("意图识别服务未初始化")
        return None
    # 对话历史记录
    dialogue = conn.dialogue
    try:
        intent_result = await conn.intent.detect_intent(conn, dialogue.dialogue, text)
        return intent_result
    except Exception as e:
        logger.bind(tag=TAG).error(f"意图识别失败: {str(e)}")
    return None
async def process_intent_result(conn, intent_result, original_text):
    """处理意图识别结果"""
    try:
        # 尝试将结果解析为JSON
        intent_data = json.loads(intent_result)
        # 检查是否有function_call
        if "function_call" in intent_data:
            # 直接从意图识别获取了function_call
            logger.bind(tag=TAG).debug(
                f"检测到function_call格式的意图结果: {intent_data['function_call']['name']}"
            )
            function_name = intent_data["function_call"]["name"]
            if function_name == "continue_chat":
                return False
            if function_name == "play_music":
                funcItem = conn.func_handler.get_function(function_name)
                if not funcItem:
                    conn.func_handler.function_registry.register_function("play_music")
            function_args = None
            if "arguments" in intent_data["function_call"]:
                function_args = intent_data["function_call"]["arguments"]
            # 确保参数是字符串格式的JSON
            if isinstance(function_args, dict):
                function_args = json.dumps(function_args)
            function_call_data = {
                "name": function_name,
                "id": str(uuid.uuid4().hex),
                "arguments": function_args,
            }
            await send_stt_message(conn, original_text)
            # 使用executor执行函数调用和结果处理
            def process_function_call():
                conn.dialogue.put(Message(role="user", content=original_text))
                result = conn.func_handler.handle_llm_function_call(
                    conn, function_call_data
                )
                if result and function_name != "play_music":
                    # 获取当前最新的文本索引
                    text = result.response
                    if text is None:
                        text = result.result
                    if text is not None:
                        text_index = (
                            conn.tts_last_text_index + 1
                            if hasattr(conn, "tts_last_text_index")
                            else 0
                        )
                        conn.recode_first_last_text(text, text_index)
                        future = conn.executor.submit(
                            conn.speak_and_play, text, text_index
                        )
                        conn.llm_finish_task = True
                        conn.tts_queue.put(future)
                        conn.dialogue.put(Message(role="assistant", content=text))
            # 将函数执行放在线程池中
            conn.executor.submit(process_function_call)
            return True
        return False
    except json.JSONDecodeError as e:
        logger.bind(tag=TAG).error(f"处理意图结果时出错: {e}")
        return False
def extract_text_in_brackets(s):
    """
    从字符串中提取中括号内的文字
    :param s: 输入字符串
    :return: 中括号内的文字，如果不存在则返回空字符串
    """
    left_bracket_index = s.find("[")
    right_bracket_index = s.find("]")
    if (
        left_bracket_index != -1
        and right_bracket_index != -1
        and left_bracket_index < right_bracket_index
    ):
        return s[left_bracket_index + 1 : right_bracket_index]
    else:
        return ""
import json
import asyncio
from config.logger import setup_logging
from plugins_func.register import (
    device_type_registry,
    register_function,
    ActionResponse,
    Action,
    ToolType,
)
TAG = __name__
logger = setup_logging()
def wrap_async_function(async_func):
    """包装异步函数为同步函数"""
    def wrapper(*args, **kwargs):
        try:
            # 获取连接对象（第一个参数）
            conn = args[0]
            if not hasattr(conn, "loop"):
                logger.bind(tag=TAG).error("Connection对象没有loop属性")
                return ActionResponse(
                    Action.ERROR,
                    "Connection对象没有loop属性",
                    "执行操作时出错: Connection对象没有loop属性",
                )
            # 使用conn对象中的事件循环
            loop = conn.loop
            # 在conn的事件循环中运行异步函数
            future = asyncio.run_coroutine_threadsafe(async_func(*args, **kwargs), loop)
            # 等待结果返回
            return future.result()
        except Exception as e:
            logger.bind(tag=TAG).error(f"运行异步函数时出错: {e}")
            return ActionResponse(Action.ERROR, str(e), f"执行操作时出错: {e}")
    return wrapper
def create_iot_function(device_name, method_name, method_info):
    """
    根据IOT设备描述生成通用的控制函数
    """
    async def iot_control_function(
        conn, response_success=None, response_failure=None, **params
    ):
        try:
            # 设置默认响应消息
            if not response_success:
                response_success = "操作成功"
            if not response_failure:
                response_failure = "操作失败"
            # 打印响应参数
            logger.bind(tag=TAG).info(
                f"控制函数接收到的响应参数: success='{response_success}', failure='{response_failure}'"
            )
            # 发送控制命令
            await send_iot_conn(conn, device_name, method_name, params)
            # 等待一小段时间让状态更新
            await asyncio.sleep(0.1)
            # 生成结果信息
            result = f"{device_name}的{method_name}操作执行成功"
            # 处理响应中可能的占位符
            response = response_success
            # 替换{value}占位符
            for param_name, param_value in params.items():
                # 先尝试直接替换参数值
                if "{" + param_name + "}" in response:
                    response = response.replace(
                        "{" + param_name + "}", str(param_value)
                    )
                # 如果有{value}占位符，用相关参数替换
                if "{value}" in response:
                    response = response.replace("{value}", str(param_value))
                    break
            return ActionResponse(Action.RESPONSE, result, response)
        except Exception as e:
            logger.bind(tag=TAG).error(f"执行{device_name}的{method_name}操作失败: {e}")
            # 操作失败时使用大模型提供的失败响应
            response = response_failure
            return ActionResponse(Action.ERROR, str(e), response)
    return wrap_async_function(iot_control_function)
def create_iot_query_function(device_name, prop_name, prop_info):
    """
    根据IOT设备属性创建查询函数
    """
    async def iot_query_function(conn, response_success=None, response_failure=None):
        try:
            # 打印响应参数
            logger.bind(tag=TAG).info(
                f"查询函数接收到的响应参数: success='{response_success}', failure='{response_failure}'"
            )
            value = await get_iot_status(conn, device_name, prop_name)
            # 查询成功，生成结果
            if value is not None:
                # 使用大模型提供的成功响应，并替换其中的占位符
                response = response_success.replace("{value}", str(value))
                return ActionResponse(Action.RESPONSE, str(value), response)
            else:
                # 查询失败，使用大模型提供的失败响应
                response = response_failure
                return ActionResponse(Action.ERROR, f"属性{prop_name}不存在", response)
        except Exception as e:
            logger.bind(tag=TAG).error(f"查询{device_name}的{prop_name}时出错: {e}")
            # 查询出错时使用大模型提供的失败响应
            response = response_failure
            return ActionResponse(Action.ERROR, str(e), response)
    return wrap_async_function(iot_query_function)
class IotDescriptor:
    """
    A class to represent an IoT descriptor.
    """
    def __init__(self, name, description, properties, methods):
        self.name = name
        self.description = description
        self.properties = []
        self.methods = []
        # 根据描述创建属性
        for key, value in properties.items():
            property_item = globals()[key] = {}
            property_item["name"] = key
            property_item["description"] = value["description"]
            if value["type"] == "number":
                property_item["value"] = 0
            elif value["type"] == "boolean":
                property_item["value"] = False
            else:
                property_item["value"] = ""
            self.properties.append(property_item)
        # 根据描述创建方法
        for key, value in methods.items():
            method = globals()[key] = {}
            method["description"] = value["description"]
            method["name"] = key
            for k, v in value["parameters"].items():
                method[k] = {}
                method[k]["description"] = v["description"]
                if v["type"] == "number":
                    method[k]["value"] = 0
                elif v["type"] == "boolean":
                    method[k]["value"] = False
                else:
                    method[k]["value"] = ""
            self.methods.append(method)
def register_device_type(descriptor):
    """注册设备类型及其功能"""
    device_name = descriptor["name"]
    type_id = device_type_registry.generate_device_type_id(descriptor)
    # 如果该类型已注册，直接返回类型ID
    if type_id in device_type_registry.type_functions:
        return type_id
    functions = {}
    # 为每个属性创建查询函数
    for prop_name, prop_info in descriptor["properties"].items():
        func_name = f"get_{device_name.lower()}_{prop_name.lower()}"
        func_desc = {
            "type": "function",
            "function": {
                "name": func_name,
                "description": f"查询{descriptor['description']}的{prop_info['description']}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "response_success": {
                            "type": "string",
                            "description": f"查询成功时的友好回复，必须使用{{value}}作为占位符表示查询到的值",
                        },
                        "response_failure": {
                            "type": "string",
                            "description": f"查询失败时的友好回复，例如：'无法获取{device_name}的{prop_info['description']}'",
                        },
                    },
                    "required": ["response_success", "response_failure"],
                },
            },
        }
        query_func = create_iot_query_function(device_name, prop_name, prop_info)
        decorated_func = register_function(func_name, func_desc, ToolType.IOT_CTL)(
            query_func
        )
        functions[func_name] = decorated_func
    # 为每个方法创建控制函数
    for method_name, method_info in descriptor["methods"].items():
        func_name = f"{device_name.lower()}_{method_name.lower()}"
        # 创建参数字典，添加原有参数
        parameters = {
            param_name: {
                "type": param_info["type"],
                "description": param_info["description"],
            }
            for param_name, param_info in method_info["parameters"].items()
        }
        # 添加响应参数
        parameters.update(
            {
                "response_success": {
                    "type": "string",
                    "description": "操作成功时的友好回复,关于该设备的操作结果，设备名称尽量使用description中的名称",
                },
                "response_failure": {
                    "type": "string",
                    "description": "操作失败时的友好回复,关于该设备的操作结果，设备名称尽量使用description中的名称",
                },
            }
        )
        # 构建必须参数列表（原有参数 + 响应参数）
        required_params = list(method_info["parameters"].keys())
        required_params.extend(["response_success", "response_failure"])
        func_desc = {
            "type": "function",
            "function": {
                "name": func_name,
                "description": f"{descriptor['description']} - {method_info['description']}",
                "parameters": {
                    "type": "object",
                    "properties": parameters,
                    "required": required_params,
                },
            },
        }
        control_func = create_iot_function(device_name, method_name, method_info)
        decorated_func = register_function(func_name, func_desc, ToolType.IOT_CTL)(
            control_func
        )
        functions[func_name] = decorated_func
    device_type_registry.register_device_type(type_id, functions)
    return type_id
# 用于接受前端设备推送的搜索iot描述
async def handleIotDescriptors(conn, descriptors):
    wait_max_time = 5
    while conn.func_handler is None or not conn.func_handler.finish_init:
        await asyncio.sleep(1)
        wait_max_time -= 1
        if wait_max_time <= 0:
            logger.bind(tag=TAG).debug("连接对象没有func_handler")
            return
    """处理物联网描述"""
    functions_changed = False
    for descriptor in descriptors:
        # 创建IOT设备描述符
        iot_descriptor = IotDescriptor(
            descriptor["name"],
            descriptor["description"],
            descriptor["properties"],
            descriptor["methods"],
        )
        conn.iot_descriptors[descriptor["name"]] = iot_descriptor
        if conn.use_function_call_mode:
            # 注册或获取设备类型
            type_id = register_device_type(descriptor)
            device_functions = device_type_registry.get_device_functions(type_id)
            # 在连接级注册设备函数
            if hasattr(conn, "func_handler"):
                for func_name in device_functions:
                    conn.func_handler.function_registry.register_function(func_name)
                    logger.bind(tag=TAG).info(
                        f"注册IOT函数到function handler: {func_name}"
                    )
                    functions_changed = True
    # 如果注册了新函数，更新function描述列表
    if functions_changed and hasattr(conn, "func_handler"):
        conn.func_handler.upload_functions_desc()
        func_names = conn.func_handler.current_support_functions()
        logger.bind(tag=TAG).info(f"设备类型: {type_id}")
        logger.bind(tag=TAG).info(
            f"更新function描述列表完成，当前支持的函数: {func_names}"
        )
async def handleIotStatus(conn, states):
    """处理物联网状态"""
    for state in states:
        for key, value in conn.iot_descriptors.items():
            if key == state["name"]:
                for property_item in value.properties:
                    for k, v in state["state"].items():
                        if property_item["name"] == k:
                            if type(v) != type(property_item["value"]):
                                logger.bind(tag=TAG).error(
                                    f"属性{property_item['name']}的值类型不匹配"
                                )
                                break
                            else:
                                property_item["value"] = v
                                logger.bind(tag=TAG).info(
                                    f"物联网状态更新: {key} , {property_item['name']} = {v}"
                                )
                            break
                break
async def get_iot_status(conn, name, property_name):
    """获取物联网状态"""
    for key, value in conn.iot_descriptors.items():
        if key == name:
            for property_item in value.properties:
                if property_item["name"] == property_name:
                    return property_item["value"]
    logger.bind(tag=TAG).warning(f"未找到设备 {name} 的属性 {property_name}")
    return None
async def set_iot_status(conn, name, property_name, value):
    """设置物联网状态"""
    for key, iot_descriptor in conn.iot_descriptors.items():
        if key == name:
            for property_item in iot_descriptor.properties:
                if property_item["name"] == property_name:
                    if type(value) != type(property_item["value"]):
                        logger.bind(tag=TAG).error(
                            f"属性{property_item['name']}的值类型不匹配"
                        )
                        return
                    property_item["value"] = value
                    logger.bind(tag=TAG).info(
                        f"物联网状态更新: {name} , {property_name} = {value}"
                    )
                    return
    logger.bind(tag=TAG).warning(f"未找到设备 {name} 的属性 {property_name}")
async def send_iot_conn(conn, name, method_name, parameters):
    """发送物联网指令"""
    for key, value in conn.iot_descriptors.items():
        if key == name:
            # 找到了设备
            for method in value.methods:
                # 找到了方法
                if method["name"] == method_name:
                    await conn.websocket.send(
                        json.dumps(
                            {
                                "type": "iot",
                                "commands": [
                                    {
                                        "name": name,
                                        "method": method_name,
                                        "parameters": parameters,
                                    }
                                ],
                            }
                        )
                    )
                    return
    logger.bind(tag=TAG).error(f"未找到方法{method_name}")
from config.logger import setup_logging
import time
import asyncio
from core.utils.util import remove_punctuation_and_length
from core.handle.sendAudioHandle import send_stt_message
from core.handle.intentHandler import handle_user_intent
TAG = __name__
logger = setup_logging()
async def handleAudioMessage(conn, audio):
    if not conn.asr_server_receive:
        logger.bind(tag=TAG).debug(f"前期数据处理中，暂停接收")
        return
    if conn.client_listen_mode == "auto":
        have_voice = conn.vad.is_vad(conn, audio)
    else:
        have_voice = conn.client_have_voice
    # 如果本次没有声音，本段也没声音，就把声音丢弃了
    if have_voice == False and conn.client_have_voice == False:
        await no_voice_close_connect(conn)
        conn.asr_audio.append(audio)
        conn.asr_audio = conn.asr_audio[
            -10:
        ]  # 保留最新的10帧音频内容，解决ASR句首丢字问题
        return
    conn.client_no_voice_last_time = 0.0
    conn.asr_audio.append(audio)
    # 如果本段有声音，且已经停止了
    if conn.client_voice_stop:
        conn.client_abort = False
        conn.asr_server_receive = False
        # 音频太短了，无法识别
        if len(conn.asr_audio) < 15:
            conn.asr_server_receive = True
        else:
            text, _ = await conn.asr.speech_to_text(conn.asr_audio, conn.session_id)
            logger.bind(tag=TAG).info(f"识别文本: {text}")
            text_len, _ = remove_punctuation_and_length(text)
            if text_len > 0:
                await startToChat(conn, text)
            else:
                conn.asr_server_receive = True
        conn.asr_audio.clear()
        conn.reset_vad_states()
async def startToChat(conn, text):
    if conn.need_bind:
        await check_bind_device(conn)
        return
    # 首先进行意图分析
    intent_handled = await handle_user_intent(conn, text)
    if intent_handled:
        # 如果意图已被处理，不再进行聊天
        conn.asr_server_receive = True
        return
    # 意图未被处理，继续常规聊天流程
    await send_stt_message(conn, text)
    if conn.use_function_call_mode:
        # 使用支持function calling的聊天方法
        conn.executor.submit(conn.chat_with_function_calling, text)
    else:
        conn.executor.submit(conn.chat, text)
async def no_voice_close_connect(conn):
    if conn.client_no_voice_last_time == 0.0:
        conn.client_no_voice_last_time = time.time() * 1000
    else:
        no_voice_time = time.time() * 1000 - conn.client_no_voice_last_time
        close_connection_no_voice_time = int(
            conn.config.get("close_connection_no_voice_time", 120)
        )
        if (
            not conn.close_after_chat
            and no_voice_time > 1000 * close_connection_no_voice_time
        ):
            conn.close_after_chat = True
            conn.client_abort = False
            conn.asr_server_receive = False
            prompt = (
                "请你以“时间过得真快”未来头，用富有感情、依依不舍的话来结束这场对话吧。"
            )
            await startToChat(conn, prompt)
async def check_bind_device(conn):
    if conn.bind_code:
        # 确保bind_code是6位数字
        if len(conn.bind_code) != 6:
            logger.bind(tag=TAG).error(f"无效的绑定码格式: {conn.bind_code}")
            text = "绑定码格式错误，请检查配置。"
            await send_stt_message(conn, text)
            return
        text = f"请登录控制面板，输入{conn.bind_code}，绑定设备。"
        await send_stt_message(conn, text)
        conn.tts_first_text_index = 0
        conn.tts_last_text_index = 6
        conn.llm_finish_task = True
        # 播放提示音
        music_path = "config/assets/bind_code.wav"
        opus_packets, _ = conn.tts.audio_to_opus_data(music_path)
        conn.audio_play_queue.put((opus_packets, text, 0))
        # 逐个播放数字
        for i in range(6):  # 确保只播放6位数字
            try:
                digit = conn.bind_code[i]
                num_path = f"config/assets/bind_code/{digit}.wav"
                num_packets, _ = conn.tts.audio_to_opus_data(num_path)
                conn.audio_play_queue.put((num_packets, text, i + 1))
            except Exception as e:
                logger.bind(tag=TAG).error(f"播放数字音频失败: {e}")
                continue
    else:
        text = f"没有找到该设备的版本信息，请正确配置 OTA地址，然后重新编译固件。"
        await send_stt_message(conn, text)
        conn.tts_first_text_index = 0
        conn.tts_last_text_index = 0
        conn.llm_finish_task = True
        music_path = "config/assets/bind_not_found.wav"
        opus_packets, _ = conn.tts.audio_to_opus_data(music_path)
        conn.audio_play_queue.put((opus_packets, text, 0))
from config.logger import setup_logging
import json
import asyncio
import time
from core.utils.util import (
    remove_punctuation_and_length,
    get_string_no_punctuation_or_emoji,
)
TAG = __name__
logger = setup_logging()
async def sendAudioMessage(conn, audios, text, text_index=0):
    # 发送句子开始消息
    if text_index == conn.tts_first_text_index:
        logger.bind(tag=TAG).info(f"发送第一段语音: {text}")
    await send_tts_message(conn, "sentence_start", text)
    # 播放音频
    await sendAudio(conn, audios)
    await send_tts_message(conn, "sentence_end", text)
    # 发送结束消息（如果是最后一个文本）
    if conn.llm_finish_task and text_index == conn.tts_last_text_index:
        await send_tts_message(conn, "stop", None)
        if conn.close_after_chat:
            await conn.close()
# 播放音频
async def sendAudio(conn, audios):
    # 流控参数优化
    frame_duration = 60  # 帧时长（毫秒），匹配 Opus 编码
    start_time = time.perf_counter()
    play_position = 0
    # 预缓冲：发送前 3 帧
    pre_buffer = min(3, len(audios))
    for i in range(pre_buffer):
        await conn.websocket.send(audios[i])
    # 正常播放剩余帧
    for opus_packet in audios[pre_buffer:]:
        if conn.client_abort:
            return
        # 计算预期发送时间
        expected_time = start_time + (play_position / 1000)
        current_time = time.perf_counter()
        delay = expected_time - current_time
        if delay > 0:
            await asyncio.sleep(delay)
        await conn.websocket.send(opus_packet)
        play_position += frame_duration
async def send_tts_message(conn, state, text=None):
    """发送 TTS 状态消息"""
    message = {"type": "tts", "state": state, "session_id": conn.session_id}
    if text is not None:
        message["text"] = text
    # TTS播放结束
    if state == "stop":
        # 播放提示音
        tts_notify = conn.config.get("enable_stop_tts_notify", False)
        if tts_notify:
            stop_tts_notify_voice = conn.config.get(
                "stop_tts_notify_voice", "config/assets/tts_notify.mp3"
            )
            audios, duration = conn.tts.audio_to_opus_data(stop_tts_notify_voice)
            await sendAudio(conn, audios)
        # 清除服务端讲话状态
        conn.clearSpeakStatus()
    # 发送消息到客户端
    await conn.websocket.send(json.dumps(message))
async def send_stt_message(conn, text):
    """发送 STT 状态消息"""
    stt_text = get_string_no_punctuation_or_emoji(text)
    await conn.websocket.send(
        json.dumps({"type": "stt", "text": stt_text, "session_id": conn.session_id})
    )
    await conn.websocket.send(
        json.dumps(
            {
                "type": "llm",
                "text": "😊",
                "emotion": "happy",
                "session_id": conn.session_id,
            }
        )
    )
    await send_tts_message(conn, "start")
from config.logger import setup_logging
import json
from core.handle.abortHandle import handleAbortMessage
from core.handle.helloHandle import handleHelloMessage
from core.utils.util import remove_punctuation_and_length
from core.handle.receiveAudioHandle import startToChat, handleAudioMessage
from core.handle.sendAudioHandle import send_stt_message, send_tts_message
from core.handle.iotHandle import handleIotDescriptors, handleIotStatus
import asyncio
TAG = __name__
logger = setup_logging()
async def handleTextMessage(conn, message):
    """处理文本消息"""
    logger.bind(tag=TAG).info(f"收到文本消息：{message}")
    try:
        msg_json = json.loads(message)
        if isinstance(msg_json, int):
            await conn.websocket.send(message)
            return
        if msg_json["type"] == "hello":
            await handleHelloMessage(conn)
        elif msg_json["type"] == "abort":
            await handleAbortMessage(conn)
        elif msg_json["type"] == "listen":
            if "mode" in msg_json:
                conn.client_listen_mode = msg_json["mode"]
                logger.bind(tag=TAG).debug(f"客户端拾音模式：{conn.client_listen_mode}")
            if msg_json["state"] == "start":
                conn.client_have_voice = True
                conn.client_voice_stop = False
            elif msg_json["state"] == "stop":
                conn.client_have_voice = True
                conn.client_voice_stop = True
                if len(conn.asr_audio) > 0:
                    await handleAudioMessage(conn, b"")
            elif msg_json["state"] == "detect":
                conn.asr_server_receive = False
                conn.client_have_voice = False
                conn.asr_audio.clear()
                if "text" in msg_json:
                    text = msg_json["text"]
                    _, text = remove_punctuation_and_length(text)
                    # 识别是否是唤醒词
                    is_wakeup_words = text in conn.config.get("wakeup_words")
                    # 是否开启唤醒词回复
                    enable_greeting = conn.config.get("enable_greeting", True)
                    if is_wakeup_words and not enable_greeting:
                        # 如果是唤醒词，且关闭了唤醒词回复，就不用回答
                        await send_stt_message(conn, text)
                        await send_tts_message(conn, "stop", None)
                    else:
                        # 否则需要LLM对文字内容进行答复
                        await startToChat(conn, text)
        elif msg_json["type"] == "iot":
            if "descriptors" in msg_json:
                asyncio.create_task(handleIotDescriptors(conn, msg_json["descriptors"]))
            if "states" in msg_json:
                asyncio.create_task(handleIotStatus(conn, msg_json["states"]))
    except json.JSONDecodeError:
        await conn.websocket.send(message)
"""MCP服务管理器"""
import os, json
from typing import Dict, Any, List
from .MCPClient import MCPClient
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType
from config.config_loader import get_project_dir
TAG = __name__
class MCPManager:
    """管理多个MCP服务的集中管理器"""
    def __init__(self, conn) -> None:
        """
        初始化MCP管理器
        """
        self.conn = conn
        self.logger = setup_logging()
        self.config_path = get_project_dir() + "data/.mcp_server_settings.json"
        if os.path.exists(self.config_path) == False:
            self.config_path = ""
            self.logger.bind(tag=TAG).warning(
                f"请检查mcp服务配置文件：data/.mcp_server_settings.json"
            )
        self.client: Dict[str, MCPClient] = {}
        self.tools = []
    def load_config(self) -> Dict[str, Any]:
        """加载MCP服务配置
        Returns:
            Dict[str, Any]: 服务配置字典
        """
        if len(self.config_path) == 0:
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("mcpServers", {})
        except Exception as e:
            self.logger.bind(tag=TAG).error(
                f"Error loading MCP config from {self.config_path}: {e}"
            )
            return {}
    async def initialize_servers(self) -> None:
        """初始化所有MCP服务"""
        config = self.load_config()
        for name, srv_config in config.items():
            if not srv_config.get("command"):
                self.logger.bind(tag=TAG).warning(
                    f"Skipping server {name}: command not specified"
                )
                continue
            try:
                client = MCPClient(srv_config)
                await client.initialize()
                self.client[name] = client
                self.logger.bind(tag=TAG).info(f"Initialized MCP client: {name}")
                client_tools = client.get_available_tools()
                self.tools.extend(client_tools)
                for tool in client_tools:
                    func_name = "mcp_" + tool["function"]["name"]
                    register_function(func_name, tool, ToolType.MCP_CLIENT)(
                        self.execute_tool
                    )
                    self.conn.func_handler.function_registry.register_function(
                        func_name
                    )
            except Exception as e:
                self.logger.bind(tag=TAG).error(
                    f"Failed to initialize MCP server {name}: {e}"
                )
        self.conn.func_handler.upload_functions_desc()
    def get_all_tools(self) -> List[Dict[str, Any]]:
        """获取所有服务的工具function定义
        Returns:
            List[Dict[str, Any]]: 所有工具的function定义列表
        """
        return self.tools
    def is_mcp_tool(self, tool_name: str) -> bool:
        """检查是否是MCP工具
        Args:
            tool_name: 工具名称
        Returns:
            bool: 是否是MCP工具
        """
        for tool in self.tools:
            if (
                tool.get("function") != None
                and tool["function"].get("name") == tool_name
            ):
                return True
        return False
    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """执行工具调用
        Args:
            tool_name: 工具名称
            arguments: 工具参数
        Returns:
            Any: 工具执行结果
        Raises:
            ValueError: 工具未找到时抛出
        """
        self.logger.bind(tag=TAG).info(
            f"Executing tool {tool_name} with arguments: {arguments}"
        )
        for client in self.client.values():
            if client.has_tool(tool_name):
                return await client.call_tool(tool_name, arguments)
        raise ValueError(f"Tool {tool_name} not found in any MCP server")
    async def cleanup_all(self) -> None:
        for name, client in self.client.items():
            try:
                await client.cleanup()
                self.logger.bind(tag=TAG).info(f"Cleaned up MCP client: {name}")
            except Exception as e:
                self.logger.bind(tag=TAG).error(
                    f"Error cleaning up MCP client {name}: {e}"
                )
        self.client.clear()
from datetime import timedelta
from typing import Optional
from contextlib import AsyncExitStack
import os, shutil
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from config.logger import setup_logging
TAG = __name__
class MCPClient:
    def __init__(self, config):
        # Initialize session and client objects
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self.logger = setup_logging()
        self.config = config
        self.tolls = []
    async def initialize(self):
        args = self.config.get("args", [])
        command = (
            shutil.which("npx")
            if self.config["command"] == "npx"
            else self.config["command"]
        )
        env={**os.environ}
        if self.config.get("env"):
            env.update(self.config["env"])
        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env
        )
        stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
        self.stdio, self.write = stdio_transport
        time_out_delta =  timedelta(seconds=15)
        self.session = await self.exit_stack.enter_async_context(ClientSession(read_stream=self.stdio, write_stream=self.write, read_timeout_seconds=time_out_delta))
        await self.session.initialize()
        # List available tools
        response = await self.session.list_tools()
        tools = response.tools
        self.tools = tools
        self.logger.bind(tag=TAG).info(f"Connected to server with tools:{[tool.name for tool in tools]}")
    def has_tool(self, tool_name):
        return any(tool.name == tool_name for tool in self.tools)
    def get_available_tools(self):
        available_tools = [{"type": "function", "function":{ 
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.inputSchema
        } } for tool in self.tools]
        return available_tools
    async def call_tool(self, tool_name: str, tool_args: dict):
        self.logger.bind(tag=TAG).info(f"MCPClient Calling tool {tool_name} with args: {tool_args}")
        try:
            response = await self.session.call_tool(tool_name, tool_args)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Error calling tool {tool_name}: {e}")
            from types import SimpleNamespace
            error_content = SimpleNamespace(
                type='text',
                text=f"Error calling tool {tool_name}: {e}"
            )
            error_response = SimpleNamespace(
                content=[error_content],
                isError=True
            )
            return error_response
        self.logger.bind(tag=TAG).info(f"MCPClient Response from tool {tool_name}: {response}")
        return response
    async def cleanup(self):
        """Clean up resources"""
        await self.exit_stack.aclose()
from abc import ABC, abstractmethod
from typing import Optional, Tuple, List
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
class ASRProviderBase(ABC):
    @abstractmethod
    def save_audio_to_file(self, opus_data: List[bytes], session_id: str) -> str:
        """解码Opus数据并保存为WAV文件"""
        pass
    @abstractmethod
    async def speech_to_text(self, opus_data: List[bytes], session_id: str) -> Tuple[Optional[str], Optional[str]]:
        """将语音数据转换为文本"""
        pass
import time
import io
import wave
import os
from typing import Optional, Tuple, List
import uuid
import websockets
import json
import gzip
import opuslib_next
from core.providers.asr.base import ASRProviderBase
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
CLIENT_FULL_REQUEST = 0b0001
CLIENT_AUDIO_ONLY_REQUEST = 0b0010
NO_SEQUENCE = 0b0000
NEG_SEQUENCE = 0b0010
SERVER_FULL_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR_RESPONSE = 0b1111
NO_SERIALIZATION = 0b0000
JSON = 0b0001
THRIFT = 0b0011
CUSTOM_TYPE = 0b1111
NO_COMPRESSION = 0b0000
GZIP = 0b0001
CUSTOM_COMPRESSION = 0b1111
def parse_response(res):
    """
    protocol_version(4 bits), header_size(4 bits),
    message_type(4 bits), message_type_specific_flags(4 bits)
    serialization_method(4 bits) message_compression(4 bits)
    reserved （8bits) 保留字段
    header_extensions 扩展头(大小等于 8 * 4 * (header_size - 1) )
    payload 类似与http 请求体
    """
    protocol_version = res[0] >> 4
    header_size = res[0] & 0x0f
    message_type = res[1] >> 4
    message_type_specific_flags = res[1] & 0x0f
    serialization_method = res[2] >> 4
    message_compression = res[2] & 0x0f
    reserved = res[3]
    header_extensions = res[4:header_size * 4]
    payload = res[header_size * 4:]
    result = {}
    payload_msg = None
    payload_size = 0
    if message_type == SERVER_FULL_RESPONSE:
        payload_size = int.from_bytes(payload[:4], "big", signed=True)
        payload_msg = payload[4:]
    elif message_type == SERVER_ACK:
        seq = int.from_bytes(payload[:4], "big", signed=True)
        result['seq'] = seq
        if len(payload) >= 8:
            payload_size = int.from_bytes(payload[4:8], "big", signed=False)
            payload_msg = payload[8:]
    elif message_type == SERVER_ERROR_RESPONSE:
        code = int.from_bytes(payload[:4], "big", signed=False)
        result['code'] = code
        payload_size = int.from_bytes(payload[4:8], "big", signed=False)
        payload_msg = payload[8:]
    if payload_msg is None:
        return result
    if message_compression == GZIP:
        payload_msg = gzip.decompress(payload_msg)
    if serialization_method == JSON:
        payload_msg = json.loads(str(payload_msg, "utf-8"))
    elif serialization_method != NO_SERIALIZATION:
        payload_msg = str(payload_msg, "utf-8")
    result['payload_msg'] = payload_msg
    result['payload_size'] = payload_size
    return result
class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        self.appid = config.get("appid")
        self.cluster = config.get("cluster")
        self.access_token = config.get("access_token")
        self.output_dir = config.get("output_dir")
        self.host = "openspeech.bytedance.com"
        self.ws_url = f"wss://{self.host}/api/v2/asr"
        self.success_code = 1000
        self.seg_duration = 15000
        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)
    def save_audio_to_file(self, opus_data: List[bytes], session_id: str) -> str:
        """将Opus音频数据解码并保存为WAV文件"""
        file_name = f"asr_{session_id}_{uuid.uuid4()}.wav"
        file_path = os.path.join(self.output_dir, file_name)
        decoder = opuslib_next.Decoder(16000, 1)  # 16kHz, 单声道
        pcm_data = []
        for opus_packet in opus_data:
            try:
                pcm_frame = decoder.decode(opus_packet, 960)  # 960 samples = 60ms
                pcm_data.append(pcm_frame)
            except opuslib_next.OpusError as e:
                logger.bind(tag=TAG).error(f"Opus解码错误: {e}", exc_info=True)
        with wave.open(file_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 2 bytes = 16-bit
            wf.setframerate(16000)
            wf.writeframes(b"".join(pcm_data))
        return file_path
    @staticmethod
    def _generate_header(message_type=CLIENT_FULL_REQUEST, message_type_specific_flags=NO_SEQUENCE) -> bytearray:
        """Generate protocol header."""
        header = bytearray()
        header_size = 1
        header.append((0b0001 << 4) | header_size)  # Protocol version
        header.append((message_type << 4) | message_type_specific_flags)
        header.append((0b0001 << 4) | 0b0001)  # JSON serialization & GZIP compression
        header.append(0x00)  # reserved
        return header
    def _construct_request(self, reqid) -> dict:
        """Construct the request payload."""
        return {
            "app": {
                "appid": f"{self.appid}",
                "cluster": self.cluster,
                "token": self.access_token,
            },
            "user": {
                "uid": str(uuid.uuid4()),
            },
            "request": {
                "reqid": reqid,
                "show_utterances": False,
                "sequence": 1
            },
            "audio": {
                "format": "wav",
                "rate": 16000,
                "language": "zh-CN",
                "bits": 16,
                "channel": 1,
                "codec": "raw",
            },
        }
    async def _send_request(self, audio_data: List[bytes], segment_size: int) -> Optional[str]:
        """Send request to Volcano ASR service."""
        try:
            auth_header = {'Authorization': 'Bearer; {}'.format(self.access_token)}
            async with websockets.connect(self.ws_url, additional_headers=auth_header) as websocket:
                # Prepare request data
                request_params = self._construct_request(str(uuid.uuid4()))
                print(request_params)
                payload_bytes = str.encode(json.dumps(request_params))
                payload_bytes = gzip.compress(payload_bytes)
                full_client_request = self._generate_header()
                full_client_request.extend((len(payload_bytes)).to_bytes(4, 'big'))  # payload size(4 bytes)
                full_client_request.extend(payload_bytes)  # payload
                # Send header and metadata
                # full_client_request
                await websocket.send(full_client_request)
                res = await websocket.recv()
                result = parse_response(res)
                if 'payload_msg' in result and result['payload_msg']['code'] != self.success_code:
                    logger.bind(tag=TAG).error(f"ASR error: {result}")
                    return None
                for seq, (chunk, last) in enumerate(self.slice_data(audio_data, segment_size), 1):
                    if last:
                        audio_only_request = self._generate_header(
                            message_type=CLIENT_AUDIO_ONLY_REQUEST,
                            message_type_specific_flags=NEG_SEQUENCE
                        )
                    else:
                        audio_only_request = self._generate_header(
                            message_type=CLIENT_AUDIO_ONLY_REQUEST
                        )
                    payload_bytes = gzip.compress(chunk)
                    audio_only_request.extend((len(payload_bytes)).to_bytes(4, 'big'))  # payload size(4 bytes)
                    audio_only_request.extend(payload_bytes)  # payload
                    # Send audio data
                    await websocket.send(audio_only_request)
                # Receive response
                response = await websocket.recv()
                result = parse_response(response)
                if 'payload_msg' in result and result['payload_msg']['code'] == self.success_code:
                    if len(result['payload_msg']['result']) > 0:
                        return result['payload_msg']['result'][0]["text"]
                    return None
                else:
                    logger.bind(tag=TAG).error(f"ASR error: {result}")
                    return None
        except Exception as e:
            logger.bind(tag=TAG).error(f"ASR request failed: {e}", exc_info=True)
            return None
    @staticmethod
    def decode_opus(opus_data: List[bytes], session_id: str) -> List[bytes]:
        decoder = opuslib_next.Decoder(16000, 1)  # 16kHz, 单声道
        pcm_data = []
        for opus_packet in opus_data:
            try:
                pcm_frame = decoder.decode(opus_packet, 960)  # 960 samples = 60ms
                pcm_data.append(pcm_frame)
            except opuslib_next.OpusError as e:
                logger.bind(tag=TAG).error(f"Opus解码错误: {e}", exc_info=True)
        return pcm_data
    @staticmethod
    def read_wav_info(data: io.BytesIO = None) -> (int, int, int, int, int):
        with io.BytesIO(data) as _f:
            wave_fp = wave.open(_f, 'rb')
            nchannels, sampwidth, framerate, nframes = wave_fp.getparams()[:4]
            wave_bytes = wave_fp.readframes(nframes)
        return nchannels, sampwidth, framerate, nframes, len(wave_bytes)
    @staticmethod
    def slice_data(data: bytes, chunk_size: int) -> (list, bool):
        """
        slice data
        :param data: wav data
        :param chunk_size: the segment size in one request
        :return: segment data, last flag
        """
        data_len = len(data)
        offset = 0
        while offset + chunk_size < data_len:
            yield data[offset: offset + chunk_size], False
            offset += chunk_size
        else:
            yield data[offset: data_len], True
    async def speech_to_text(self, opus_data: List[bytes], session_id: str) -> Tuple[Optional[str], Optional[str]]:
        """将语音数据转换为文本"""
        try:
            # 合并所有opus数据包
            pcm_data = self.decode_opus(opus_data, session_id)
            combined_pcm_data = b''.join(pcm_data)
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, "wb") as wav_file:
                wav_file.setnchannels(1)  # 设置声道数
                wav_file.setsampwidth(2)  # 设置采样宽度
                wav_file.setframerate(16000)  # 设置采样率
                wav_file.writeframes(combined_pcm_data)  # 写入 PCM 数据
            # 获取封装后的 WAV 数据
            wav_data = wav_buffer.getvalue()
            nchannels, sampwidth, framerate, nframes, wav_len = self.read_wav_info(wav_data)
            size_per_sec = nchannels * sampwidth * framerate
            segment_size = int(size_per_sec * self.seg_duration / 1000)
            # 语音识别
            start_time = time.time()
            text = await self._send_request(wav_data, segment_size)
            if text:
                logger.bind(tag=TAG).debug(f"语音识别耗时: {time.time() - start_time:.3f}s | 结果: {text}")
                return text, None
            return "", None
        except Exception as e:
            logger.bind(tag=TAG).error(f"语音识别失败: {e}", exc_info=True)
            return "", None
import time
import wave
import os
import sys
import io
from config.logger import setup_logging
from typing import Optional, Tuple, List
import uuid
import opuslib_next
from core.providers.asr.base import ASRProviderBase
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess
TAG = __name__
logger = setup_logging()
# 捕获标准输出
class CaptureOutput:
    def __enter__(self):
        self._output = io.StringIO()
        self._original_stdout = sys.stdout
        sys.stdout = self._output
    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout = self._original_stdout
        self.output = self._output.getvalue()
        self._output.close()
        # 将捕获到的内容通过 logger 输出
        if self.output:
            logger.bind(tag=TAG).info(self.output.strip())
class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        self.model_dir = config.get("model_dir")
        self.output_dir = config.get("output_dir")  # 修正配置键名
        self.delete_audio_file = delete_audio_file
        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)
        with CaptureOutput():
            self.model = AutoModel(
                model=self.model_dir,
                vad_kwargs={"max_single_segment_time": 30000},
                disable_update=True,
                hub="hf"
                # device="cuda:0",  # 启用GPU加速
            )
    def save_audio_to_file(self, opus_data: List[bytes], session_id: str) -> str:
        """将Opus音频数据解码并保存为WAV文件"""
        file_name = f"asr_{session_id}_{uuid.uuid4()}.wav"
        file_path = os.path.join(self.output_dir, file_name)
        decoder = opuslib_next.Decoder(16000, 1)  # 16kHz, 单声道
        pcm_data = []
        for opus_packet in opus_data:
            try:
                pcm_frame = decoder.decode(opus_packet, 960)  # 960 samples = 60ms
                pcm_data.append(pcm_frame)
            except opuslib_next.OpusError as e:
                logger.bind(tag=TAG).error(f"Opus解码错误: {e}", exc_info=True)
        with wave.open(file_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 2 bytes = 16-bit
            wf.setframerate(16000)
            wf.writeframes(b"".join(pcm_data))
        return file_path
    async def speech_to_text(self, opus_data: List[bytes], session_id: str) -> Tuple[Optional[str], Optional[str]]:
        """语音转文本主处理逻辑"""
        file_path = None
        try:
            # 保存音频文件
            start_time = time.time()
            file_path = self.save_audio_to_file(opus_data, session_id)
            logger.bind(tag=TAG).debug(f"音频文件保存耗时: {time.time() - start_time:.3f}s | 路径: {file_path}")
            # 语音识别
            start_time = time.time()
            result = self.model.generate(
                input=file_path,
                cache={},
                language="auto",
                use_itn=True,
                batch_size_s=60,
            )
            text = rich_transcription_postprocess(result[0]["text"])
            logger.bind(tag=TAG).debug(f"语音识别耗时: {time.time() - start_time:.3f}s | 结果: {text}")
            return text, file_path
        except Exception as e:
            logger.bind(tag=TAG).error(f"语音识别失败: {e}", exc_info=True)
            return "", None
        finally:
            # 文件清理逻辑
            if self.delete_audio_file and file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.bind(tag=TAG).debug(f"已删除临时音频文件: {file_path}")
                except Exception as e:
                    logger.bind(tag=TAG).error(f"文件删除失败: {file_path} | 错误: {e}")
import time
import wave
import os
import sys
import io
from config.logger import setup_logging
from typing import Optional, Tuple, List
import uuid
import opuslib_next
from core.providers.asr.base import ASRProviderBase
import numpy as np
import sherpa_onnx
from modelscope.hub.file_download import model_file_download
TAG = __name__
logger = setup_logging()
# 捕获标准输出
class CaptureOutput:
    def __enter__(self):
        self._output = io.StringIO()
        self._original_stdout = sys.stdout
        sys.stdout = self._output
    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout = self._original_stdout
        self.output = self._output.getvalue()
        self._output.close()
        # 将捕获到的内容通过 logger 输出
        if self.output:
            logger.bind(tag=TAG).info(self.output.strip())
class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        self.model_dir = config.get("model_dir")
        self.output_dir = config.get("output_dir")
        self.delete_audio_file = delete_audio_file
        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)
        # 初始化模型文件路径
        model_files = {
            "model.int8.onnx": os.path.join(self.model_dir, "model.int8.onnx"),
            "tokens.txt": os.path.join(self.model_dir, "tokens.txt")
        }
        # 下载并检查模型文件
        try:
            for file_name, file_path in model_files.items():
                if not os.path.isfile(file_path):
                    logger.bind(tag=TAG).info(f"正在下载模型文件: {file_name}")
                    model_file_download(
                        model_id="pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue",
                        file_path=file_name,
                        local_dir=self.model_dir
                    )
                    if not os.path.isfile(file_path):
                        raise FileNotFoundError(f"模型文件下载失败: {file_path}")
            self.model_path = model_files["model.int8.onnx"]
            self.tokens_path = model_files["tokens.txt"]
        except Exception as e:
            logger.bind(tag=TAG).error(f"模型文件处理失败: {str(e)}")
            raise
        with CaptureOutput():
            self.model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=self.model_path,
                tokens=self.tokens_path,
                num_threads=2,
                sample_rate=16000,
                feature_dim=80,
                decoding_method="greedy_search",
                debug=False,
                use_itn=True,
            )
    def save_audio_to_file(self, opus_data: List[bytes], session_id: str) -> str:
        """将Opus音频数据解码并保存为WAV文件"""
        file_name = f"asr_{session_id}_{uuid.uuid4()}.wav"
        file_path = os.path.join(self.output_dir, file_name)
        decoder = opuslib_next.Decoder(16000, 1)  # 16kHz, 单声道
        pcm_data = []
        for opus_packet in opus_data:
            try:
                pcm_frame = decoder.decode(opus_packet, 960)  # 960 samples = 60ms
                pcm_data.append(pcm_frame)
            except opuslib_next.OpusError as e:
                logger.bind(tag=TAG).error(f"Opus解码错误: {e}", exc_info=True)
        with wave.open(file_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 2 bytes = 16-bit
            wf.setframerate(16000)
            wf.writeframes(b"".join(pcm_data))
        return file_path
    def read_wave(self, wave_filename: str) -> Tuple[np.ndarray, int]:
        """
        Args:
        wave_filename:
            Path to a wave file. It should be single channel and each sample should
            be 16-bit. Its sample rate does not need to be 16kHz.
        Returns:
        Return a tuple containing:
        - A 1-D array of dtype np.float32 containing the samples, which are
        normalized to the range [-1, 1].
        - sample rate of the wave file
        """
        with wave.open(wave_filename) as f:
            assert f.getnchannels() == 1, f.getnchannels()
            assert f.getsampwidth() == 2, f.getsampwidth()  # it is in bytes
            num_samples = f.getnframes()
            samples = f.readframes(num_samples)
            samples_int16 = np.frombuffer(samples, dtype=np.int16)
            samples_float32 = samples_int16.astype(np.float32)
            samples_float32 = samples_float32 / 32768
            return samples_float32, f.getframerate()
    async def speech_to_text(self, opus_data: List[bytes], session_id: str) -> Tuple[Optional[str], Optional[str]]:
        """语音转文本主处理逻辑"""
        file_path = None
        try:
            # 保存音频文件
            start_time = time.time()
            file_path = self.save_audio_to_file(opus_data, session_id)
            logger.bind(tag=TAG).debug(f"音频文件保存耗时: {time.time() - start_time:.3f}s | 路径: {file_path}")
            # 语音识别
            start_time = time.time()
            s = self.model.create_stream()
            samples, sample_rate = self.read_wave(file_path)
            s.accept_waveform(sample_rate, samples)
            self.model.decode_stream(s)
            text = s.result.text
            logger.bind(tag=TAG).debug(f"语音识别耗时: {time.time() - start_time:.3f}s | 结果: {text}")
            return text, file_path
        except Exception as e:
            logger.bind(tag=TAG).error(f"语音识别失败: {e}", exc_info=True)
            return "", None
        finally:
            # 文件清理逻辑
            if self.delete_audio_file and file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.bind(tag=TAG).debug(f"已删除临时音频文件: {file_path}")
                except Exception as e:
                    logger.bind(tag=TAG).error(f"文件删除失败: {file_path} | 错误: {e}")
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
import os
import uuid
from typing import Optional, Tuple, List
import wave
import opuslib_next
import requests
from core.providers.asr.base import ASRProviderBase
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
class ASRProvider(ASRProviderBase):
    API_URL = "https://asr.tencentcloudapi.com"
    API_VERSION = "2019-06-14"
    FORMAT = "pcm"  # 支持的音频格式：pcm, wav, mp3
    def __init__(self, config: dict, delete_audio_file: bool = True):
        self.secret_id = config.get("secret_id")
        self.secret_key = config.get("secret_key")
        self.output_dir = config.get("output_dir")
        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)
    def save_audio_to_file(self, opus_data: List[bytes], session_id: str) -> str:
        """将Opus音频数据解码并保存为WAV文件"""
        file_name = f"tencent_asr_{session_id}_{uuid.uuid4()}.wav"
        file_path = os.path.join(self.output_dir, file_name)
        decoder = opuslib_next.Decoder(16000, 1)  # 16kHz, 单声道
        pcm_data = []
        for opus_packet in opus_data:
            try:
                pcm_frame = decoder.decode(opus_packet, 960)  # 960 samples = 60ms
                pcm_data.append(pcm_frame)
            except opuslib_next.OpusError as e:
                logger.bind(tag=TAG).error(f"Opus解码错误: {e}", exc_info=True)
        with wave.open(file_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 2 bytes = 16-bit
            wf.setframerate(16000)
            wf.writeframes(b"".join(pcm_data))
        return file_path
    @staticmethod
    def decode_opus(opus_data: List[bytes]) -> bytes:
        """将Opus音频数据解码为PCM数据"""
        import opuslib_next
        decoder = opuslib_next.Decoder(16000, 1)  # 16kHz, 单声道
        pcm_data = []
        for opus_packet in opus_data:
            try:
                pcm_frame = decoder.decode(opus_packet, 960)  # 960 samples = 60ms
                pcm_data.append(pcm_frame)
            except opuslib_next.OpusError as e:
                logger.bind(tag=TAG).error(f"Opus解码错误: {e}", exc_info=True)
        return b"".join(pcm_data)
    async def speech_to_text(self, opus_data: List[bytes], session_id: str) -> Tuple[Optional[str], Optional[str]]:
        """将语音数据转换为文本"""
        if not opus_data:
            logger.bind(tag=TAG).warn("音频数据为空！")
            return None, None
        try:
            # 检查配置是否已设置
            if not self.secret_id or not self.secret_key:
                logger.bind(tag=TAG).error("腾讯云语音识别配置未设置，无法进行识别")
                return None, None
            # 将Opus音频数据解码为PCM
            pcm_data = self.decode_opus(opus_data)
            # 将音频数据转换为Base64编码
            base64_audio = base64.b64encode(pcm_data).decode('utf-8')
            # 构建请求体
            request_body = self._build_request_body(base64_audio)
            # 获取认证头
            timestamp, authorization = self._get_auth_headers(request_body)
            # 发送请求
            start_time = time.time()
            result = self._send_request(request_body, timestamp, authorization)
            if result:
                logger.bind(tag=TAG).debug(f"腾讯云语音识别耗时: {time.time() - start_time:.3f}s | 结果: {result}")
            return result, None
        except Exception as e:
            logger.bind(tag=TAG).error(f"处理音频时发生错误！{e}", exc_info=True)
            return None, None
    def _build_request_body(self, base64_audio: str) -> str:
        """构建请求体"""
        request_map = {
            "ProjectId": 0,
            "SubServiceType": 2,  # 一句话识别
            "EngSerViceType": "16k_zh",  # 中文普通话通用
            "SourceType": 1,  # 音频数据来源为语音文件
            "VoiceFormat": self.FORMAT,  # 音频格式
            "Data": base64_audio,  # Base64编码的音频数据
            "DataLen": len(base64_audio)  # 数据长度
        }
        return json.dumps(request_map)
    def _get_auth_headers(self, request_body: str) -> Tuple[str, str]:
        """获取认证头"""
        try:
            # 获取当前UTC时间戳
            now = datetime.now(timezone.utc)
            timestamp = str(int(now.timestamp()))
            date = now.strftime("%Y-%m-%d")
            # 服务名称必须是 "asr"
            service = "asr"
            # 拼接凭证范围
            credential_scope = f"{date}/{service}/tc3_request"
            # 使用TC3-HMAC-SHA256签名方法
            algorithm = "TC3-HMAC-SHA256"
            # 构建规范请求字符串
            http_request_method = "POST"
            canonical_uri = "/"
            canonical_query_string = ""
            # 注意：头部信息需要按照ASCII升序排列，且key和value都转为小写
            # 必须包含content-type和host头部
            content_type = "application/json; charset=utf-8"
            host = "asr.tencentcloudapi.com"
            action = "SentenceRecognition"  # 接口名称
            # 构建规范头部信息，注意顺序和格式
            canonical_headers = f"content-type:{content_type.lower()}\n" + \
                               f"host:{host.lower()}\n" + \
                               f"x-tc-action:{action.lower()}\n"
            signed_headers = "content-type;host;x-tc-action"
            # 请求体哈希值
            payload_hash = self._sha256_hex(request_body)
            # 构建规范请求字符串
            canonical_request = f"{http_request_method}\n" + \
                               f"{canonical_uri}\n" + \
                               f"{canonical_query_string}\n" + \
                               f"{canonical_headers}\n" + \
                               f"{signed_headers}\n" + \
                               f"{payload_hash}"
            # 计算规范请求的哈希值
            hashed_canonical_request = self._sha256_hex(canonical_request)
            # 构建待签名字符串
            string_to_sign = f"{algorithm}\n" + \
                            f"{timestamp}\n" + \
                            f"{credential_scope}\n" + \
                            f"{hashed_canonical_request}"
            # 计算签名密钥
            secret_date = self._hmac_sha256(f"TC3{self.secret_key}", date)
            secret_service = self._hmac_sha256(secret_date, service)
            secret_signing = self._hmac_sha256(secret_service, "tc3_request")
            # 计算签名
            signature = self._bytes_to_hex(self._hmac_sha256(secret_signing, string_to_sign))
            # 构建授权头
            authorization = f"{algorithm} " + \
                           f"Credential={self.secret_id}/{credential_scope}, " + \
                           f"SignedHeaders={signed_headers}, " + \
                           f"Signature={signature}"
            return timestamp, authorization
        except Exception as e:
            logger.bind(tag=TAG).error(f"生成认证头失败: {e}", exc_info=True)
            raise RuntimeError(f"生成认证头失败: {e}")
    def _send_request(self, request_body: str, timestamp: str, authorization: str) -> Optional[str]:
        """发送请求到腾讯云API"""
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Host": "asr.tencentcloudapi.com",
            "Authorization": authorization,
            "X-TC-Action": "SentenceRecognition",
            "X-TC-Version": self.API_VERSION,
            "X-TC-Timestamp": timestamp,
            "X-TC-Region": "ap-shanghai"
        }
        try:
            response = requests.post(self.API_URL, headers=headers, data=request_body)
            if not response.ok:
                raise IOError(f"请求失败: {response.status_code} {response.reason}")
            response_json = response.json()
            # 检查是否有错误
            if "Response" in response_json and "Error" in response_json["Response"]:
                error = response_json["Response"]["Error"]
                error_code = error["Code"]
                error_message = error["Message"]
                raise IOError(f"API返回错误: {error_code}: {error_message}")
            # 提取识别结果
            if "Response" in response_json and "Result" in response_json["Response"]:
                return response_json["Response"]["Result"]
            else:
                logger.bind(tag=TAG).warn(f"响应中没有识别结果: {response_json}")
                return ""
        except Exception as e:
            logger.bind(tag=TAG).error(f"发送请求失败: {e}", exc_info=True)
            return None
    def _sha256_hex(self, data: str) -> str:
        """计算字符串的SHA256哈希值"""
        digest = hashlib.sha256(data.encode('utf-8')).digest()
        return self._bytes_to_hex(digest)
    def _hmac_sha256(self, key, data: str) -> bytes:
        """计算HMAC-SHA256"""
        if isinstance(key, str):
            key = key.encode('utf-8')
        return hmac.new(key, data.encode('utf-8'), hashlib.sha256).digest()
    def _bytes_to_hex(self, bytes_data: bytes) -> str:
        """字节数组转十六进制字符串"""
        return ''.join(f"{b:02x}" for b in bytes_data)
from abc import ABC, abstractmethod
from typing import List, Dict
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
class IntentProviderBase(ABC):
    def __init__(self, config):
        self.config = config
        self.intent_options = [
            {
                "name": "handle_exit_intent",
                "desc": "结束聊天, 用户发来如再见之类的表示结束的话, 不想再进行对话的时候",
            },
            {
                "name": "play_music",
                "desc": "播放音乐, 用户希望你可以播放音乐, 只用于播放音乐的意图",
            },
            {"name": "get_time", "desc": "获取今天日期或者当前时间信息"},
            {"name": "continue_chat", "desc": "继续聊天"},
        ]
    def set_llm(self, llm):
        self.llm = llm
        # 获取模型名称和类型信息
        model_name = getattr(llm, "model_name", str(llm.__class__.__name__))
        # 记录更详细的日志
        logger.bind(tag=TAG).info(f"意图识别设置LLM: {model_name}")
    @abstractmethod
    async def detect_intent(self, conn, dialogue_history: List[Dict], text: str) -> str:
        """
        检测用户最后一句话的意图
        Args:
            dialogue_history: 对话历史记录列表，每条记录包含role和content
        Returns:
            返回识别出的意图，格式为:
            - "继续聊天"
            - "结束聊天"
            - "播放音乐 歌名" 或 "随机播放音乐"
            - "查询天气 地点名" 或 "查询天气 [当前位置]"
        """
        pass
from ..base import IntentProviderBase
from typing import List, Dict
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
class IntentProvider(IntentProviderBase):
    async def detect_intent(self, conn, dialogue_history: List[Dict], text: str) -> str:
        """
        默认的意图识别实现，始终返回继续聊天
        Args:
            dialogue_history: 对话历史记录列表
            text: 本次对话记录
        Returns:
            固定返回"继续聊天"
        """
        logger.bind(tag=TAG).debug(
            "Using functionCallProvider, always returning continue chat"
        )
        return '{"function_call": {"name": "continue_chat"}}'
from typing import List, Dict
from ..base import IntentProviderBase
from plugins_func.functions.play_music import initialize_music_handler
from config.logger import setup_logging
import re
import json
import hashlib
import time
TAG = __name__
logger = setup_logging()
class IntentProvider(IntentProviderBase):
    def __init__(self, config):
        super().__init__(config)
        self.llm = None
        self.promot = self.get_intent_system_prompt()
        # 添加缓存管理
        self.intent_cache = {}  # 缓存意图识别结果
        self.cache_expiry = 600  # 缓存有效期10分钟
        self.cache_max_size = 100  # 最多缓存100个意图
    def get_intent_system_prompt(self) -> str:
        """
        根据配置的意图选项动态生成系统提示词
        Returns:
            格式化后的系统提示词
        """
        prompt = (
            "你是一个意图识别助手。请分析用户的最后一句话，判断用户意图属于以下哪一类：\n"
            "<start>"
            f"{str(self.intent_options)}"
            "<end>\n"
            "处理步骤:"
            "1. 思考意图类型，生成function_call格式"
            "\n\n"
            "返回格式示例：\n"
            '1. 播放音乐意图: {"function_call": {"name": "play_music", "arguments": {"song_name": "音乐名称"}}}\n'
            '2. 结束对话意图: {"function_call": {"name": "handle_exit_intent", "arguments": {"say_goodbye": "goodbye"}}}\n'
            '3. 获取当天日期时间: {"function_call": {"name": "get_time"}}\n'
            '4. 继续聊天意图: {"function_call": {"name": "continue_chat"}}\n'
            "\n"
            "注意:\n"
            '- 播放音乐：无歌名时，song_name设为"random"\n'
            "- 如果没有明显的意图，应按照继续聊天意图处理\n"
            "- 只返回纯JSON，不要任何其他内容\n"
            "\n"
            "示例分析:\n"
            "```\n"
            "用户: 你也太搞笑了\n"
            '返回: {"function_call": {"name": "continue_chat"}}\n'
            "```\n"
            "```\n"
            "用户: 现在是几号了?现在几点了？\n"
            '返回: {"function_call": {"name": "get_time"}}\n'
            "```\n"
            "```\n"
            "用户: 我们明天再聊吧\n"
            '返回: {"function_call": {"name": "handle_exit_intent"}}\n'
            "```\n"
            "```\n"
            "用户: 播放中秋月\n"
            '返回: {"function_call": {"name": "play_music", "arguments": {"song_name": "中秋月"}}}\n'
            "```\n"
            "```\n"
            "可用的音乐名称:\n"
        )
        return prompt
    def clean_cache(self):
        """清理过期缓存"""
        now = time.time()
        # 找出过期键
        expired_keys = [
            k
            for k, v in self.intent_cache.items()
            if now - v["timestamp"] > self.cache_expiry
        ]
        for key in expired_keys:
            del self.intent_cache[key]
        # 如果缓存太大，移除最旧的条目
        if len(self.intent_cache) > self.cache_max_size:
            # 按时间戳排序并保留最新的条目
            sorted_items = sorted(
                self.intent_cache.items(), key=lambda x: x[1]["timestamp"]
            )
            for key, _ in sorted_items[: len(sorted_items) - self.cache_max_size]:
                del self.intent_cache[key]
    async def detect_intent(self, conn, dialogue_history: List[Dict], text: str) -> str:
        if not self.llm:
            raise ValueError("LLM provider not set")
        # 记录整体开始时间
        total_start_time = time.time()
        # 打印使用的模型信息
        model_info = getattr(self.llm, "model_name", str(self.llm.__class__.__name__))
        logger.bind(tag=TAG).debug(f"使用意图识别模型: {model_info}")
        # 计算缓存键
        cache_key = hashlib.md5(text.encode()).hexdigest()
        # 检查缓存
        if cache_key in self.intent_cache:
            cache_entry = self.intent_cache[cache_key]
            # 检查缓存是否过期
            if time.time() - cache_entry["timestamp"] <= self.cache_expiry:
                cache_time = time.time() - total_start_time
                logger.bind(tag=TAG).debug(
                    f"使用缓存的意图: {cache_key} -> {cache_entry['intent']}, 耗时: {cache_time:.4f}秒"
                )
                return cache_entry["intent"]
        # 清理缓存
        self.clean_cache()
        # 构建用户最后一句话的提示
        msgStr = ""
        # 只使用最后两句即可
        if len(dialogue_history) >= 2:
            # 保证最少有两句话的时候处理
            msgStr += f"{dialogue_history[-2].role}: {dialogue_history[-2].content}\n"
        msgStr += f"{dialogue_history[-1].role}: {dialogue_history[-1].content}\n"
        msgStr += f"User: {text}\n"
        user_prompt = f"当前的对话如下：\n{msgStr}"
        music_config = initialize_music_handler(conn)
        music_file_names = music_config["music_file_names"]
        prompt_music = f"{self.promot}\n<start>{music_file_names}\n<end>"
        logger.bind(tag=TAG).debug(f"User prompt: {prompt_music}")
        # 记录预处理完成时间
        preprocess_time = time.time() - total_start_time
        logger.bind(tag=TAG).debug(f"意图识别预处理耗时: {preprocess_time:.4f}秒")
        # 使用LLM进行意图识别
        llm_start_time = time.time()
        logger.bind(tag=TAG).debug(f"开始LLM意图识别调用, 模型: {model_info}")
        intent = self.llm.response_no_stream(
            system_prompt=prompt_music, user_prompt=user_prompt
        )
        # 记录LLM调用完成时间
        llm_time = time.time() - llm_start_time
        logger.bind(tag=TAG).debug(
            f"LLM意图识别完成, 模型: {model_info}, 调用耗时: {llm_time:.4f}秒"
        )
        # 记录后处理开始时间
        postprocess_start_time = time.time()
        # 清理和解析响应
        intent = intent.strip()
        # 尝试提取JSON部分
        match = re.search(r"\{.*\}", intent, re.DOTALL)
        if match:
            intent = match.group(0)
        # 记录总处理时间
        total_time = time.time() - total_start_time
        logger.bind(tag=TAG).debug(
            f"【意图识别性能】模型: {model_info}, 总耗时: {total_time:.4f}秒, LLM调用: {llm_time:.4f}秒, 查询: '{text[:20]}...'"
        )
        # 尝试解析为JSON
        try:
            intent_data = json.loads(intent)
            # 如果包含function_call，则格式化为适合处理的格式
            if "function_call" in intent_data:
                function_data = intent_data["function_call"]
                function_name = function_data.get("name")
                function_args = function_data.get("arguments", {})
                # 记录识别到的function call
                logger.bind(tag=TAG).info(
                    f"识别到function call: {function_name}, 参数: {function_args}"
                )
                # 添加到缓存
                self.intent_cache[cache_key] = {
                    "intent": intent,
                    "timestamp": time.time(),
                }
                # 后处理时间
                postprocess_time = time.time() - postprocess_start_time
                logger.bind(tag=TAG).debug(f"意图后处理耗时: {postprocess_time:.4f}秒")
                # 确保返回完全序列化的JSON字符串
                return intent
            else:
                # 添加到缓存
                self.intent_cache[cache_key] = {
                    "intent": intent,
                    "timestamp": time.time(),
                }
                # 后处理时间
                postprocess_time = time.time() - postprocess_start_time
                logger.bind(tag=TAG).debug(f"意图后处理耗时: {postprocess_time:.4f}秒")
                # 返回普通意图
                return intent
        except json.JSONDecodeError:
            # 后处理时间
            postprocess_time = time.time() - postprocess_start_time
            logger.bind(tag=TAG).error(
                f"无法解析意图JSON: {intent}, 后处理耗时: {postprocess_time:.4f}秒"
            )
            # 如果解析失败，默认返回继续聊天意图
            return '{"intent": "继续聊天"}'
from ..base import IntentProviderBase
from typing import List, Dict
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
class IntentProvider(IntentProviderBase):
    async def detect_intent(self, conn, dialogue_history: List[Dict], text: str) -> str:
        """
        默认的意图识别实现，始终返回继续聊天
        Args:
            dialogue_history: 对话历史记录列表
            text: 本次对话记录
        Returns:
            固定返回"继续聊天"
        """
        logger.bind(tag=TAG).debug(
            "Using NoIntentProvider, always returning continue chat"
        )
        return '{"function_call": {"name": "continue_chat"}}'
from config.logger import setup_logging
from http import HTTPStatus
from dashscope import Application
from core.providers.llm.base import LLMProviderBase
TAG = __name__
logger = setup_logging()
class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.api_key = config["api_key"]
        self.app_id = config["app_id"]
        self.base_url = config.get("base_url")
        self.is_No_prompt = config.get("is_no_prompt")
        self.memory_id = config.get("ali_memory_id")
    def response(self, session_id, dialogue):
        try:
            # 处理dialogue
            if self.is_No_prompt:
                dialogue.pop(0)
                logger.bind(tag=TAG).debug(f"【阿里百练API服务】处理后的dialogue: {dialogue}")
            # 构造调用参数
            call_params = {
                "api_key": self.api_key,
                "app_id": self.app_id,
                "session_id": session_id,
                "messages": dialogue
            }
            if self.memory_id != False:
                # 百练memory需要prompt参数
                prompt = dialogue[-1].get("content")
                call_params["memory_id"] = self.memory_id
                call_params["prompt"] = prompt
                logger.bind(tag=TAG).debug(f"【阿里百练API服务】处理后的prompt: {prompt}")
            responses = Application.call(**call_params)
            if responses.status_code != HTTPStatus.OK:
                logger.bind(tag=TAG).error(
                    f"code={responses.status_code}, "
                    f"message={responses.message}, "
                    f"请参考文档：https://help.aliyun.com/zh/model-studio/developer-reference/error-code"
                )
                yield "【阿里百练API服务响应异常】"
            else:
                logger.bind(tag=TAG).debug(f"【阿里百练API服务】构造参数: {call_params}")
                yield responses.output.text
        except Exception as e:
            logger.bind(tag=TAG).error(f"【阿里百练API服务】响应异常: {e}")
            yield "【LLM服务响应异常】"
from abc import ABC, abstractmethod
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
class LLMProviderBase(ABC):
    @abstractmethod
    def response(self, session_id, dialogue):
        """LLM response generator"""
        pass
    def response_no_stream(self, system_prompt, user_prompt):
        try:
            # 构造对话格式
            dialogue = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
            result = ""
            for part in self.response("", dialogue):
                result += part
            return result
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in Ollama response generation: {e}")
            return "【LLM服务响应异常】"
    def response_with_functions(self, session_id, dialogue, functions=None):
        """
        Default implementation for function calling (streaming)
        This should be overridden by providers that support function calls
        Returns: generator that yields either text tokens or a special function call token
        """
        # For providers that don't support functions, just return regular response
        for token in self.response(session_id, dialogue):
            yield {"type": "content", "content": token}
from config.logger import setup_logging
import requests
import json
import re
from core.providers.llm.base import LLMProviderBase
import os
# official coze sdk for Python [cozepy](https://github.com/coze-dev/coze-py)
from cozepy import COZE_CN_BASE_URL
from cozepy import Coze, TokenAuth, Message, ChatStatus, MessageContentType, ChatEventType  # noqa
TAG = __name__
logger = setup_logging()
class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.personal_access_token = config.get("personal_access_token")
        self.bot_id = config.get("bot_id")
        self.user_id = config.get("user_id")
        self.session_conversation_map = {}  # 存储session_id和conversation_id的映射
    def response(self, session_id, dialogue):
        coze_api_token = self.personal_access_token
        coze_api_base = COZE_CN_BASE_URL
        last_msg = next(m for m in reversed(dialogue) if m["role"] == "user")
        coze = Coze(auth=TokenAuth(token=coze_api_token), base_url=coze_api_base)
        conversation_id = self.session_conversation_map.get(session_id)
        # 如果没有找到conversation_id，则创建新的对话
        if not conversation_id:
            conversation = coze.conversations.create(
                messages=[
                ]
            )
            conversation_id = conversation.id
            self.session_conversation_map[session_id] = conversation_id  # 更新映射
        for event in coze.chat.stream(
            bot_id=self.bot_id,
            user_id=self.user_id,
            additional_messages=[
                Message.build_user_question_text(last_msg["content"]),
            ],
            conversation_id=conversation_id,
        ):
            if event.event == ChatEventType.CONVERSATION_MESSAGE_DELTA:
                print(event.message.content, end="", flush=True)
                yield event.message.content
import json
from config.logger import setup_logging
import requests
from core.providers.llm.base import LLMProviderBase
TAG = __name__
logger = setup_logging()
class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.api_key = config["api_key"]
        self.mode = config.get("mode", "chat-messages")
        self.base_url = config.get("base_url", "https://api.dify.ai/v1").rstrip("/")
        self.session_conversation_map = {}  # 存储session_id和conversation_id的映射
    def response(self, session_id, dialogue):
        try:
            # 取最后一条用户消息
            last_msg = next(m for m in reversed(dialogue) if m["role"] == "user")
            conversation_id = self.session_conversation_map.get(session_id)
            # 发起流式请求
            if self.mode == "chat-messages":
                request_json = {
                    "query": last_msg["content"],
                    "response_mode": "streaming",
                    "user": session_id,
                    "inputs": {},
                    "conversation_id": conversation_id,
                }
            elif self.mode == "workflows/run":
                request_json = {
                    "inputs": {"query": last_msg["content"]},
                    "response_mode": "streaming",
                    "user": session_id,
                }
            elif self.mode == "completion-messages":
                request_json = {
                    "inputs": {"query": last_msg["content"]},
                    "response_mode": "streaming",
                    "user": session_id,
                }
            with requests.post(
                f"{self.base_url}/{self.mode}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=request_json,
                stream=True,
            ) as r:
                if self.mode == "chat-messages":
                    for line in r.iter_lines():
                        if line.startswith(b"data: "):
                            event = json.loads(line[6:])
                            # 如果没有找到conversation_id，则获取此次conversation_id
                            if not conversation_id:
                                conversation_id = event.get("conversation_id")
                                self.session_conversation_map[session_id] = (
                                    conversation_id  # 更新映射
                                )
                            # 过滤 message_replace 事件，此事件会全量推一次
                            if event.get("event") != "message_replace" and event.get("answer"):
                                yield event["answer"]
                elif self.mode == "workflows/run":
                    for line in r.iter_lines():
                        if line.startswith(b"data: "):
                            event = json.loads(line[6:])
                            if event.get("event") == "workflow_finished":
                                if event["data"]["status"] == "succeeded":
                                    yield event["data"]["outputs"]["answer"]
                                else:
                                    yield "【服务响应异常】"
                elif self.mode == "completion-messages":
                    for line in r.iter_lines():
                        if line.startswith(b"data: "):
                            event = json.loads(line[6:])
                            # 过滤 message_replace 事件，此事件会全量推一次
                            if event.get("event") != "message_replace" and event.get("answer"):
                                yield event["answer"]
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in response generation: {e}")
            yield "【服务响应异常】"
import json
from config.logger import setup_logging
import requests
from core.providers.llm.base import LLMProviderBase
TAG = __name__
logger = setup_logging()
class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.api_key = config["api_key"]
        self.base_url = config.get("base_url")
        self.detail = config.get("detail", False)
        self.variables = config.get("variables", {})
    def response(self, session_id, dialogue):
        try:
            # 取最后一条用户消息
            last_msg = next(m for m in reversed(dialogue) if m["role"] == "user")
            # 发起流式请求
            with requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "stream": True,
                        "chatId": session_id,
                        "detail": self.detail,
                        "variables": self.variables,
                        "messages": [
                            {
                                "role": "user",
                                "content": last_msg["content"]
                            }
                        ]
                    },
                    stream=True
            ) as r:
                for line in r.iter_lines():
                    if line:
                        try:
                            if line.startswith(b'data: '):
                                if line[6:].decode('utf-8') == '[DONE]':
                                    break
                                data = json.loads(line[6:])
                                if 'choices' in data and len(data['choices']) > 0:
                                    delta = data['choices'][0].get('delta', {})
                                    if delta and 'content' in delta and delta['content'] is not None:
                                        content = delta['content']
                                        if '<think>' in content:
                                            continue
                                        if '</think>' in content:
                                            continue
                                        yield content
                        except json.JSONDecodeError as e:
                            continue
                        except Exception as e:
                            continue
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in response generation: {e}")
            yield "【服务响应异常】"
import google.generativeai as genai
from core.utils.util import check_model_key
from core.providers.llm.base import LLMProviderBase
from config.logger import setup_logging
import requests
import json
TAG = __name__
logger = setup_logging()
class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        """初始化Gemini LLM Provider"""
        self.model_name = config.get("model_name", "gemini-1.5-pro")
        self.api_key = config.get("api_key")
        self.http_proxy = config.get("http_proxy")
        self.https_proxy = config.get("https_proxy")
        have_key = check_model_key("LLM", self.api_key)
        if not have_key:
            return
        try:
            # 初始化Gemini客户端
            # 配置代理（如果提供了代理配置）
            self.proxies = None
            if self.http_proxy is not "" or self.https_proxy is not "":
                self.proxies = {
                    "http": self.http_proxy,
                    "https": self.https_proxy,
                }
                logger.bind(tag=TAG).info(f"Gemini set proxys:{self.proxies}")
                # 使用猴子补丁修改 google-generativeai 库的请求会话
                # 使用 session 对象配置 genai
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel(self.model_name)
            # 设置生成参数
            self.generation_config = {
                "temperature": 0.7,
                "top_p": 0.9,
                "top_k": 40,
                "max_output_tokens": 2048,
            }
            self.chat = None
        except Exception as e:
            logger.bind(tag=TAG).error(f"Gemini初始化失败: {e}")
            self.model = None
    def response(self, session_id, dialogue):
        """生成Gemini对话响应"""
        if not self.model:
            yield "【Gemini服务未正确初始化】"
            return
        try:
            # 处理对话历史
            chat_history = []
            for msg in dialogue[:-1]:  # 历史对话
                role = "model" if msg["role"] == "assistant" else "user"
                content = msg["content"].strip()
                if content:
                    chat_history.append({"role": role, "parts": [{"text": content}]})
            # 获取当前消息
            current_msg = dialogue[-1]["content"]
            # 构建请求体
            request_body = {
                "contents": chat_history
                + [{"role": "user", "parts": [{"text": current_msg}]}],
                "generationConfig": self.generation_config,
            }
            # 构建请求URL
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent?key={self.api_key}"
            # 构建请求头
            headers = {
                "Content-Type": "application/json",
            }
            # 发送POST请求,经测试手动 request 无法使用 stream 模式
            if self.proxies:
                response = requests.post(
                    url,
                    headers=headers,
                    json=request_body,
                    stream=False,
                    proxies=self.proxies,
                )
                try:
                    data = response.json()  # 直接解析JSON
                    if "candidates" in data and data["candidates"]:
                        yield data["candidates"][0]["content"]["parts"][0]["text"]
                    else:
                        yield "未找到候选回复。"
                except json.JSONDecodeError as e:
                    yield f"JSON解码错误：{e}"
                except Exception as e:
                    yield f"发生错误：{e}"
            else:
                logger.bind(tag=TAG).info(f"Gemini stream mode ")
                chat = self.model.start_chat(history=chat_history)
                # 发送消息并获取流式响应
                response = chat.send_message(
                    current_msg, stream=True, generation_config=self.generation_config
                )
                # 处理流式响应
                for chunk in response:
                    if hasattr(chunk, "text") and chunk.text:
                        yield chunk.text
        except Exception as e:
            error_msg = str(e)
            logger.bind(tag=TAG).error(f"Gemini响应生成错误: {error_msg}")
            # 针对不同错误返回友好提示
            if "Rate limit" in error_msg:
                yield "【Gemini服务请求太频繁,请稍后再试】"
            elif "Invalid API key" in error_msg:
                yield "【Gemini API key无效】"
            else:
                yield f"【Gemini服务响应异常: {error_msg}】"
        except requests.exceptions.RequestException as e:
            yield f"请求失败：{e}"
        except json.JSONDecodeError as e:
            yield f"JSON解码错误：{e}"
        except Exception as e:
            yield f"发生错误：{e}"
from config.logger import setup_logging
from openai import OpenAI
import json
from core.providers.llm.base import LLMProviderBase
TAG = __name__
logger = setup_logging()
class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.base_url = config.get("base_url", "http://localhost:11434")
        # Initialize OpenAI client with Ollama base URL
        # 如果没有v1，增加v1
        if not self.base_url.endswith("/v1"):
            self.base_url = f"{self.base_url}/v1"
        self.client = OpenAI(
            base_url=self.base_url,
            api_key="ollama"  # Ollama doesn't need an API key but OpenAI client requires one
        )
    def response(self, session_id, dialogue):
        try:
            responses = self.client.chat.completions.create(
                model=self.model_name,
                messages=dialogue,
                stream=True
            )
            is_active=True
            for chunk in responses:
                try:
                    delta = chunk.choices[0].delta if getattr(chunk, 'choices', None) else None
                    content = delta.content if hasattr(delta, 'content') else ''
                    if content:
                        if '<think>' in content:
                            is_active = False
                            content = content.split('<think>')[0]
                        if '</think>' in content:
                            is_active = True
                            content = content.split('</think>')[-1]
                        if is_active:
                            yield content
                except Exception as e:
                    logger.bind(tag=TAG).error(f"Error processing chunk: {e}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in Ollama response generation: {e}")
            yield "【Ollama服务响应异常】"
    def response_with_functions(self, session_id, dialogue, functions=None):
        try:
            stream = self.client.chat.completions.create(
                model=self.model_name,
                messages=dialogue,
                stream=True,
                tools=functions,
            )
            for chunk in stream:
                yield chunk.choices[0].delta.content, chunk.choices[0].delta.tool_calls
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in Ollama function call: {e}")
            yield {"type": "content", "content": f"【Ollama服务响应异常: {str(e)}】"}
import openai
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.llm.base import LLMProviderBase
TAG = __name__
logger = setup_logging()
class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.api_key = config.get("api_key")
        if "base_url" in config:
            self.base_url = config.get("base_url")
        else:
            self.base_url = config.get("url")
        max_tokens = config.get("max_tokens")
        if max_tokens is None or max_tokens == "":
            max_tokens = 500
        try:
            max_tokens = int(max_tokens)
        except (ValueError, TypeError):
            max_tokens = 500
        self.max_tokens = max_tokens
        check_model_key("LLM", self.api_key)
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
    def response(self, session_id, dialogue):
        try:
            responses = self.client.chat.completions.create(
                model=self.model_name,
                messages=dialogue,
                stream=True,
                max_tokens=self.max_tokens,
            )
            is_active = True
            for chunk in responses:
                try:
                    # 检查是否存在有效的choice且content不为空
                    delta = (
                        chunk.choices[0].delta
                        if getattr(chunk, "choices", None)
                        else None
                    )
                    content = delta.content if hasattr(delta, "content") else ""
                except IndexError:
                    content = ""
                if content:
                    # 处理标签跨多个chunk的情况
                    if "<think>" in content:
                        is_active = False
                        content = content.split("<think>")[0]
                    if "</think>" in content:
                        is_active = True
                        content = content.split("</think>")[-1]
                    if is_active:
                        yield content
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in response generation: {e}")
    def response_with_functions(self, session_id, dialogue, functions=None):
        try:
            stream = self.client.chat.completions.create(
                model=self.model_name, messages=dialogue, stream=True, tools=functions
            )
            for chunk in stream:
                yield chunk.choices[0].delta.content, chunk.choices[0].delta.tool_calls
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in function call streaming: {e}")
            yield {"type": "content", "content": f"【OpenAI服务响应异常: {e}】"}
from config.logger import setup_logging
from openai import OpenAI
import json
from core.providers.llm.base import LLMProviderBase
TAG = __name__
logger = setup_logging()
class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.base_url = config.get("base_url", "http://localhost:9997")
        # Initialize OpenAI client with Xinference base URL
        # 如果没有v1，增加v1
        if not self.base_url.endswith("/v1"):
            self.base_url = f"{self.base_url}/v1"
        logger.bind(tag=TAG).info(f"Initializing Xinference LLM provider with model: {self.model_name}, base_url: {self.base_url}")
        try:
            self.client = OpenAI(
                base_url=self.base_url,
                api_key="xinference"  # Xinference has a similar setup to Ollama where it doesn't need an actual key
            )
            logger.bind(tag=TAG).info("Xinference client initialized successfully")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error initializing Xinference client: {e}")
            raise
    def response(self, session_id, dialogue):
        try:
            logger.bind(tag=TAG).debug(f"Sending request to Xinference with model: {self.model_name}, dialogue length: {len(dialogue)}")
            responses = self.client.chat.completions.create(
                model=self.model_name,
                messages=dialogue,
                stream=True
            )
            is_active=True
            for chunk in responses:
                try:
                    delta = chunk.choices[0].delta if getattr(chunk, 'choices', None) else None
                    content = delta.content if hasattr(delta, 'content') else ''
                    if content:
                        if '<think>' in content:
                            is_active = False
                            content = content.split('<think>')[0]
                        if '</think>' in content:
                            is_active = True
                            content = content.split('</think>')[-1]
                        if is_active:
                            yield content
                except Exception as e:
                    logger.bind(tag=TAG).error(f"Error processing chunk: {e}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in Xinference response generation: {e}")
            yield "【Xinference服务响应异常】"
    def response_with_functions(self, session_id, dialogue, functions=None):
        try:
            logger.bind(tag=TAG).debug(f"Sending function call request to Xinference with model: {self.model_name}, dialogue length: {len(dialogue)}")
            if functions:
                logger.bind(tag=TAG).debug(f"Function calls enabled with: {[f.get('function', {}).get('name') for f in functions]}")
            stream = self.client.chat.completions.create(
                model=self.model_name,
                messages=dialogue,
                stream=True,
                tools=functions,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta
                content = delta.content
                tool_calls = delta.tool_calls
                if content:
                    yield content, tool_calls
                elif tool_calls:
                    yield None, tool_calls
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in Xinference function call: {e}")
            yield {"type": "content", "content": f"【Xinference服务响应异常: {str(e)}】"}
from abc import ABC, abstractmethod
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
class MemoryProviderBase(ABC):
    def __init__(self, config):
        self.config = config
        self.role_id = None
        self.llm = None
    @abstractmethod
    async def save_memory(self, msgs):
        """Save a new memory for specific role and return memory ID"""
        print("this is base func", msgs)
    @abstractmethod
    async def query_memory(self, query: str) -> str:
        """Query memories for specific role based on similarity"""
        return "please implement query method"
    def init_memory(self, role_id, llm):
        self.role_id = role_id    
        self.llm = llm
from ..base import MemoryProviderBase, logger
import time
import json
import os
import yaml
from config.config_loader import get_project_dir
short_term_memory_prompt = """
# 时空记忆编织者
## 核心使命
构建可生长的动态记忆网络，在有限空间内保留关键信息的同时，智能维护信息演变轨迹
根据对话记录，总结user的重要信息，以便在未来的对话中提供更个性化的服务
## 记忆法则
### 1. 三维度记忆评估（每次更新必执行）
| 维度       | 评估标准                  | 权重分 |
|------------|---------------------------|--------|
| 时效性     | 信息新鲜度（按对话轮次） | 40%    |
| 情感强度   | 含💖标记/重复提及次数     | 35%    |
| 关联密度   | 与其他信息的连接数量      | 25%    |
### 2. 动态更新机制
**名字变更处理示例：**
原始记忆："曾用名": ["张三"], "现用名": "张三丰"
触发条件：当检测到「我叫X」「称呼我Y」等命名信号时
操作流程：
1. 将旧名移入"曾用名"列表
2. 记录命名时间轴："2024-02-15 14:32:启用张三丰"
3. 在记忆立方追加：「从张三到张三丰的身份蜕变」
### 3. 空间优化策略
- **信息压缩术**：用符号体系提升密度
  - ✅"张三丰[北/软工/🐱]"
  - ❌"北京软件工程师，养猫"
- **淘汰预警**：当总字数≥900时触发
  1. 删除权重分<60且3轮未提及的信息
  2. 合并相似条目（保留时间戳最近的）
## 记忆结构
输出格式必须为可解析的json字符串，不需要解释、注释和说明，保存记忆时仅从对话提取信息，不要混入示例内容
```json
{
  "时空档案": {
    "身份图谱": {
      "现用名": "",
      "特征标记": [] 
    },
    "记忆立方": [
      {
        "事件": "入职新公司",
        "时间戳": "2024-03-20",
        "情感值": 0.9,
        "关联项": ["下午茶"],
        "保鲜期": 30 
      }
    ]
  },
  "关系网络": {
    "高频话题": {"职场": 12},
    "暗线联系": [""]
  },
  "待响应": {
    "紧急事项": ["需立即处理的任务"], 
    "潜在关怀": ["可主动提供的帮助"]
  },
  "高光语录": [
    "最打动人心的瞬间，强烈的情感表达，user的原话"
  ]
}
```
"""
def extract_json_data(json_code):
    start = json_code.find("```json")
    # 从start开始找到下一个```结束
    end = json_code.find("```", start + 1)
    # print("start:", start, "end:", end)
    if start == -1 or end == -1:
        try:
            jsonData = json.loads(json_code)
            return json_code
        except Exception as e:
            print("Error:", e)
        return ""
    jsonData = json_code[start + 7 : end]
    return jsonData
TAG = __name__
class MemoryProvider(MemoryProviderBase):
    def __init__(self, config):
        super().__init__(config)
        self.short_momery = ""
        self.memory_path = get_project_dir() + "data/.memory.yaml"
        self.load_memory()
    def init_memory(self, role_id, llm):
        super().init_memory(role_id, llm)
        self.load_memory()
    def load_memory(self):
        all_memory = {}
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                all_memory = yaml.safe_load(f) or {}
        if self.role_id in all_memory:
            self.short_momery = all_memory[self.role_id]
    def save_memory_to_file(self):
        all_memory = {}
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                all_memory = yaml.safe_load(f) or {}
        all_memory[self.role_id] = self.short_momery
        with open(self.memory_path, "w", encoding="utf-8") as f:
            yaml.dump(all_memory, f, allow_unicode=True)
    async def save_memory(self, msgs):
        if self.llm is None:
            logger.bind(tag=TAG).error("LLM is not set for memory provider")
            return None
        if len(msgs) < 2:
            return None
        msgStr = ""
        for msg in msgs:
            if msg.role == "user":
                msgStr += f"User: {msg.content}\n"
            elif msg.role == "assistant":
                msgStr += f"Assistant: {msg.content}\n"
        if len(self.short_momery) > 0:
            msgStr += "历史记忆：\n"
            msgStr += self.short_momery
        # 当前时间
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        msgStr += f"当前时间：{time_str}"
        result = self.llm.response_no_stream(short_term_memory_prompt, msgStr)
        json_str = extract_json_data(result)
        try:
            json_data = json.loads(json_str)  # 检查json格式是否正确
            self.short_momery = json_str
        except Exception as e:
            print("Error:", e)
        self.save_memory_to_file()
        logger.bind(tag=TAG).info(f"Save memory successful - Role: {self.role_id}")
        return self.short_momery
    async def query_memory(self, query: str) -> str:
        return self.short_momery
import traceback
from ..base import MemoryProviderBase, logger
from mem0 import MemoryClient
from core.utils.util import check_model_key
TAG = __name__
class MemoryProvider(MemoryProviderBase):
    def __init__(self, config):
        super().__init__(config)
        self.api_key = config.get("api_key", "")
        self.api_version = config.get("api_version", "v1.1")
        have_key = check_model_key("Mem0ai", self.api_key)
        if not have_key:
            self.use_mem0 = False
            return
        else:
            self.use_mem0 = True
        try:
            self.client = MemoryClient(api_key=self.api_key)
            logger.bind(tag=TAG).info("成功连接到 Mem0ai 服务")
        except Exception as e:
            logger.bind(tag=TAG).error(f"连接到 Mem0ai 服务时发生错误: {str(e)}")
            logger.bind(tag=TAG).error(f"详细错误: {traceback.format_exc()}")
            self.use_mem0 = False
    async def save_memory(self, msgs):
        if not self.use_mem0:
            return None
        if len(msgs) < 2:
            return None
        try:
            # Format the content as a message list for mem0
            messages = [
                {"role": message.role, "content": message.content}
                for message in msgs
                if message.role != "system"
            ]
            result = self.client.add(
                messages, user_id=self.role_id, output_format=self.api_version
            )
            logger.bind(tag=TAG).debug(f"Save memory result: {result}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"保存记忆失败: {str(e)}")
            return None
    async def query_memory(self, query: str) -> str:
        if not self.use_mem0:
            return ""
        try:
            results = self.client.search(
                query, user_id=self.role_id, output_format=self.api_version
            )
            if not results or "results" not in results:
                return ""
            # Format each memory entry with its update time up to minutes
            memories = []
            for entry in results["results"]:
                timestamp = entry.get("updated_at", "")
                if timestamp:
                    try:
                        # Parse and reformat the timestamp
                        dt = timestamp.split(".")[0]  # Remove milliseconds
                        formatted_time = dt.replace("T", " ")
                    except:
                        formatted_time = timestamp
                memory = entry.get("memory", "")
                if timestamp and memory:
                    # Store tuple of (timestamp, formatted_string) for sorting
                    memories.append((timestamp, f"[{formatted_time}] {memory}"))
            # Sort by timestamp in descending order (newest first)
            memories.sort(key=lambda x: x[0], reverse=True)
            # Extract only the formatted strings
            memories_str = "\n".join(f"- {memory[1]}" for memory in memories)
            logger.bind(tag=TAG).debug(f"Query results: {memories_str}")
            return memories_str
        except Exception as e:
            logger.bind(tag=TAG).error(f"查询记忆失败: {str(e)}")
            return ""
'''
不使用记忆，可以选择此模块
'''
from ..base import MemoryProviderBase, logger
TAG = __name__
class MemoryProvider(MemoryProviderBase):
    def __init__(self, config):
        super().__init__(config)
    async def save_memory(self, msgs):
        logger.bind(tag=TAG).debug("nomem mode: No memory saving is performed.")
        return None
    async def query_memory(self, query: str)-> str:
        logger.bind(tag=TAG).debug("nomem mode: No memory query is performed.")
        return ""
import os
import uuid
import json
import hmac
import hashlib
import base64
import requests
from datetime import datetime
from core.providers.tts.base import TTSProviderBase
import http.client
import urllib.parse
import time
import uuid
from urllib import parse
class AccessToken:
    @staticmethod
    def _encode_text(text):
        encoded_text = parse.quote_plus(text)
        return encoded_text.replace('+', '%20').replace('*', '%2A').replace('%7E', '~')
    @staticmethod
    def _encode_dict(dic):
        keys = dic.keys()
        dic_sorted = [(key, dic[key]) for key in sorted(keys)]
        encoded_text = parse.urlencode(dic_sorted)
        return encoded_text.replace('+', '%20').replace('*', '%2A').replace('%7E', '~')
    @staticmethod
    def create_token(access_key_id, access_key_secret):
        parameters = {'AccessKeyId': access_key_id,
                      'Action': 'CreateToken',
                      'Format': 'JSON',
                      'RegionId': 'cn-shanghai',
                      'SignatureMethod': 'HMAC-SHA1',
                      'SignatureNonce': str(uuid.uuid1()),
                      'SignatureVersion': '1.0',
                      'Timestamp': time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      'Version': '2019-02-28'}
        # 构造规范化的请求字符串
        query_string = AccessToken._encode_dict(parameters)
        # print('规范化的请求字符串: %s' % query_string)
        # 构造待签名字符串
        string_to_sign = 'GET' + '&' + AccessToken._encode_text('/') + '&' + AccessToken._encode_text(query_string)
        # print('待签名的字符串: %s' % string_to_sign)
        # 计算签名
        secreted_string = hmac.new(bytes(access_key_secret + '&', encoding='utf-8'),
                                   bytes(string_to_sign, encoding='utf-8'),
                                   hashlib.sha1).digest()
        signature = base64.b64encode(secreted_string)
        # print('签名: %s' % signature)
        # 进行URL编码
        signature = AccessToken._encode_text(signature)
        # print('URL编码后的签名: %s' % signature)
        # 调用服务
        full_url = 'http://nls-meta.cn-shanghai.aliyuncs.com/?Signature=%s&%s' % (signature, query_string)
        # print('url: %s' % full_url)
        # 提交HTTP GET请求
        response = requests.get(full_url)
        if response.ok:
            root_obj = response.json()
            key = 'Token'
            if key in root_obj:
                token = root_obj[key]['Id']
                expire_time = root_obj[key]['ExpireTime']
                return token, expire_time
        # print(response.text)
        return None, None
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        # 新增空值判断逻辑
        self.access_key_id = config.get("access_key_id")
        self.access_key_secret = config.get("access_key_secret")
        self.appkey = config.get("appkey")
        self.format = config.get("format", "wav")
        self.sample_rate = config.get("sample_rate", 16000)
        self.voice = config.get("voice", "xiaoyun")
        self.volume = config.get("volume", 50)
        self.speech_rate = config.get("speech_rate", 0)
        self.pitch_rate = config.get("pitch_rate", 0)
        self.host = config.get("host", "nls-gateway-cn-shanghai.aliyuncs.com")
        self.api_url = f"https://{self.host}/stream/v1/tts"
        self.header = {
            "Content-Type": "application/json"
        }
        if self.access_key_id and self.access_key_secret:
            # 使用密钥对生成临时token
            self._refresh_token()
        else:
            # 直接使用预生成的长期token
            self.token = config.get("token")
            self.expire_time = None
    def _refresh_token(self):
        """刷新Token并记录过期时间"""
        if self.access_key_id and self.access_key_secret:
            self.token, expire_time_str = AccessToken.create_token(
                self.access_key_id, 
                self.access_key_secret
            )
            if not expire_time_str:
                raise ValueError("无法获取有效的Token过期时间")
            try:
                #统一转换为字符串处理
                expire_str = str(expire_time_str).strip()
                if expire_str.isdigit():
                    expire_time = datetime.fromtimestamp(int(expire_str))
                else:
                    expire_time = datetime.strptime(
                        expire_str, 
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                self.expire_time = expire_time.timestamp() - 60
            except Exception as e:
                raise ValueError(f"无效的过期时间格式: {expire_str}") from e
        else:
            self.expire_time = None
        if not self.token:
            raise ValueError("无法获取有效的访问Token")
    def _is_token_expired(self):
        """检查Token是否过期"""
        if not self.expire_time:
            return False  # 长期Token不过期
        # 新增调试日志
        # current_time = time.time()
        # remaining = self.expire_time - current_time
        # print(f"Token过期检查: 当前时间 {datetime.fromtimestamp(current_time)} | "
        #              f"过期时间 {datetime.fromtimestamp(self.expire_time)} | "
        #              f"剩余 {remaining:.2f}秒")
        return time.time() > self.expire_time
    def generate_filename(self, extension=".wav"):
        return os.path.join(self.output_file, f"tts-{__name__}{datetime.now().date()}@{uuid.uuid4().hex}{extension}")
    async def text_to_speak(self, text, output_file):
        if self._is_token_expired():
            logger.warning("Token已过期，正在自动刷新...")
            self._refresh_token()
        request_json = {
            "appkey": self.appkey,
            "token": self.token,
            "text": text,
            "format": self.format,
            "sample_rate": self.sample_rate,
            "voice": self.voice,
            "volume": self.volume,
            "speech_rate": self.speech_rate,
            "pitch_rate": self.pitch_rate
        }
        # print(self.api_url, json.dumps(request_json, ensure_ascii=False))
        try:
            resp = requests.post(self.api_url, json.dumps(request_json), headers=self.header)
            if resp.status_code == 401:  # Token过期特殊处理
                self._refresh_token()
                resp = requests.post(self.api_url, json.dumps(request_json), headers=self.header)
            # 检查返回请求数据的mime类型是否是audio/***，是则保存到指定路径下；返回的是binary格式的
            if resp.headers['Content-Type'].startswith('audio/'):
                with open(output_file, 'wb') as f:
                    f.write(resp.content)
                return output_file
            else:
                raise Exception(f"{__name__} status_code: {resp.status_code} response: {resp.content}")
        except Exception as e:
            raise Exception(f"{__name__} error: {e}")
import asyncio
from config.logger import setup_logging
import os
import numpy as np
import opuslib_next
from pydub import AudioSegment
from abc import ABC, abstractmethod
from core.utils.tts import MarkdownCleaner
TAG = __name__
logger = setup_logging()
class TTSProviderBase(ABC):
    def __init__(self, config, delete_audio_file):
        self.delete_audio_file = delete_audio_file
        self.output_file = config.get("output_dir")
    @abstractmethod
    def generate_filename(self):
        pass
    def to_tts(self, text):
        tmp_file = self.generate_filename()
        try:
            max_repeat_time = 5
            text = MarkdownCleaner.clean_markdown(text)
            while not os.path.exists(tmp_file) and max_repeat_time > 0:
                asyncio.run(self.text_to_speak(text, tmp_file))
                if not os.path.exists(tmp_file):
                    max_repeat_time = max_repeat_time - 1
                    logger.bind(tag=TAG).error(f"语音生成失败: {text}:{tmp_file}，再试{max_repeat_time}次")
            if max_repeat_time > 0:
                logger.bind(tag=TAG).info(f"语音生成成功: {text}:{tmp_file}，重试{5 - max_repeat_time}次")
            return tmp_file
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to generate TTS file: {e}")
            return None
    @abstractmethod
    async def text_to_speak(self, text, output_file):
        pass
    def audio_to_opus_data(self, audio_file_path):
        """音频文件转换为Opus编码"""
        # 获取文件后缀名
        file_type = os.path.splitext(audio_file_path)[1]
        if file_type:
            file_type = file_type.lstrip('.')
        # 读取音频文件，-nostdin 参数：不要从标准输入读取数据，否则FFmpeg会阻塞
        audio = AudioSegment.from_file(audio_file_path, format=file_type, parameters=["-nostdin"])
        # 转换为单声道/16kHz采样率/16位小端编码（确保与编码器匹配）
        audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
        # 音频时长(秒)
        duration = len(audio) / 1000.0
        # 获取原始PCM数据（16位小端）
        raw_data = audio.raw_data
        # 初始化Opus编码器
        encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
        # 编码参数
        frame_duration = 60  # 60ms per frame
        frame_size = int(16000 * frame_duration / 1000)  # 960 samples/frame
        opus_datas = []
        # 按帧处理所有音频数据（包括最后一帧可能补零）
        for i in range(0, len(raw_data), frame_size * 2):  # 16bit=2bytes/sample
            # 获取当前帧的二进制数据
            chunk = raw_data[i:i + frame_size * 2]
            # 如果最后一帧不足，补零
            if len(chunk) < frame_size * 2:
                chunk += b'\x00' * (frame_size * 2 - len(chunk))
            # 转换为numpy数组处理
            np_frame = np.frombuffer(chunk, dtype=np.int16)
            # 编码Opus数据
            opus_data = encoder.encode(np_frame.tobytes(), frame_size)
            opus_datas.append(opus_data)
        return opus_datas, duration
import os
import uuid
import json
import base64
import requests
from datetime import datetime
from core.providers.tts.base import TTSProviderBase
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.model = config.get("model")
        self.access_token = config.get("access_token")
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice")
        self.response_format = config.get("response_format")
        self.host = "api.coze.cn"
        self.api_url = f"https://{self.host}/v1/audio/speech"
    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )
    async def text_to_speak(self, text, output_file):
        request_json = {
            "model": self.model,
            "input": text,
            "voice_id": self.voice,
            "response_format": self.response_format,
        }
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        response = requests.request(
            "POST", self.api_url, json=request_json, headers=headers
        )
        data = response.content
        file_to_save = open(output_file, "wb")
        file_to_save.write(data)
import os
import uuid
import requests
from config.logger import setup_logging
from datetime import datetime
from core.providers.tts.base import TTSProviderBase
TAG = __name__
logger = setup_logging()
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.url = config.get("url")
        self.headers = config.get("headers", {})
        self.params = config.get("params")
        self.format = config.get("format", "wav")
        self.output_file = config.get("output_dir", "tmp/")
    def generate_filename(self):
        return os.path.join(self.output_file, f"tts-{datetime.now().date()}@{uuid.uuid4().hex}.{self.format}")
    async def text_to_speak(self, text, output_file):
        request_params = {}
        for k, v in self.params.items():
            if isinstance(v, str) and "{prompt_text}" in v:
                v = v.replace("{prompt_text}", text)
            request_params[k] = v
        resp = requests.get(self.url, params=request_params, headers=self.headers)
        if resp.status_code == 200:
            with open(output_file, "wb") as file:
                file.write(resp.content)
        else:
            logger.bind(tag=TAG).error(f"Custom TTS请求失败: {resp.status_code} - {resp.text}")
import os
import uuid
import json
import base64
import requests
from datetime import datetime
from core.utils.util import check_model_key
from core.providers.tts.base import TTSProviderBase
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.appid = config.get("appid")
        self.access_token = config.get("access_token")
        self.cluster = config.get("cluster")
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice")
        self.api_url = config.get("api_url")
        self.authorization = config.get("authorization")
        self.header = {"Authorization": f"{self.authorization}{self.access_token}"}
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
                "token": "access_token",
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
import os
import uuid
import edge_tts
from datetime import datetime
from core.providers.tts.base import TTSProviderBase
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice")
    def generate_filename(self, extension=".mp3"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )
    async def text_to_speak(self, text, output_file):
        communicate = edge_tts.Communicate(text, voice=self.voice)
        # 确保目录存在并创建空文件
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, "wb") as f:
            pass
        # 流式写入音频数据
        with open(output_file, "ab") as f:  # 改为追加模式避免覆盖
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":  # 只处理音频数据块
                    f.write(chunk["data"])
import base64
import os
import uuid
import requests
import ormsgpack
from pathlib import Path
from pydantic import BaseModel, Field, conint, model_validator
from typing_extensions import Annotated
from datetime import datetime
from typing import Literal
from core.utils.util import check_model_key
from core.providers.tts.base import TTSProviderBase
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
class ServeReferenceAudio(BaseModel):
    audio: bytes
    text: str
    @model_validator(mode="before")
    def decode_audio(cls, values):
        audio = values.get("audio")
        if (
            isinstance(audio, str) and len(audio) > 255
        ):  # Check if audio is a string (Base64)
            try:
                values["audio"] = base64.b64decode(audio)
            except Exception as e:
                # If the audio is not a valid base64 string, we will just ignore it and let the server handle it
                pass
        return values
    def __repr__(self) -> str:
        return f"ServeReferenceAudio(text={self.text!r}, audio_size={len(self.audio)})"
class ServeTTSRequest(BaseModel):
    text: str
    chunk_length: Annotated[int, conint(ge=100, le=300, strict=True)] = 200
    # Audio format
    format: Literal["wav", "pcm", "mp3"] = "wav"
    # References audios for in-context learning
    references: list[ServeReferenceAudio] = []
    # Reference id
    # For example, if you want use https://fish.audio/m/7f92f8afb8ec43bf81429cc1c9199cb1/
    # Just pass 7f92f8afb8ec43bf81429cc1c9199cb1
    reference_id: str | None = None
    seed: int | None = None
    use_memory_cache: Literal["on", "off"] = "off"
    # Normalize text for en & zh, this increase stability for numbers
    normalize: bool = True
    # not usually used below
    streaming: bool = False
    max_new_tokens: int = 1024
    top_p: Annotated[float, Field(ge=0.1, le=1.0, strict=True)] = 0.7
    repetition_penalty: Annotated[float, Field(ge=0.9, le=2.0, strict=True)] = 1.2
    temperature: Annotated[float, Field(ge=0.1, le=1.0, strict=True)] = 0.7
    class Config:
        # Allow arbitrary types for pytorch related types
        arbitrary_types_allowed = True
def audio_to_bytes(file_path):
    if not file_path or not Path(file_path).exists():
        return None
    with open(file_path, "rb") as wav_file:
        wav = wav_file.read()
    return wav
def read_ref_text(ref_text):
    path = Path(ref_text)
    if path.exists() and path.is_file():
        with path.open("r", encoding="utf-8") as file:
            return file.read()
    return ref_text
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.reference_id = config.get("reference_id")
        self.reference_audio = config.get("reference_audio", [])
        self.reference_text = config.get("reference_text", [])
        self.format = config.get("format", "wav")
        self.channels = int(config.get("channels", 1))
        self.rate = int(config.get("rate", 44100))
        self.api_key = config.get("api_key", "YOUR_API_KEY")
        have_key = check_model_key("FishSpeech TTS", self.api_key)
        if not have_key:
            return
        self.normalize = config.get("normalize", True)
        self.max_new_tokens = int(config.get("max_new_tokens", 1024))
        self.chunk_length = int(config.get("chunk_length", 200))
        self.top_p = float(config.get("top_p", 0.7))
        self.repetition_penalty = float(config.get("repetition_penalty", 1.2))
        self.temperature = float(config.get("temperature", 0.7))
        self.streaming = bool(config.get("streaming", False))
        self.use_memory_cache = config.get("use_memory_cache", "on")
        self.seed = config.get("seed")
        self.api_url = config.get("api_url", "http://127.0.0.1:8080/v1/tts")
    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )
    async def text_to_speak(self, text, output_file):
        # Prepare reference data
        byte_audios = [audio_to_bytes(ref_audio) for ref_audio in self.reference_audio]
        ref_texts = [read_ref_text(ref_text) for ref_text in self.reference_text]
        data = {
            "text": text,
            "references": [
                ServeReferenceAudio(audio=audio if audio else b"", text=text)
                for text, audio in zip(ref_texts, byte_audios)
            ],
            "reference_id": self.reference_id,
            "normalize": self.normalize,
            "format": self.format,
            "max_new_tokens": self.max_new_tokens,
            "chunk_length": self.chunk_length,
            "top_p": self.top_p,
            "repetition_penalty": self.repetition_penalty,
            "temperature": self.temperature,
            "streaming": self.streaming,
            "use_memory_cache": self.use_memory_cache,
            "seed": self.seed,
        }
        pydantic_data = ServeTTSRequest(**data)
        response = requests.post(
            self.api_url,
            data=ormsgpack.packb(
                pydantic_data, option=ormsgpack.OPT_SERIALIZE_PYDANTIC
            ),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/msgpack",
            },
        )
        if response.status_code == 200:
            audio_content = response.content
            with open(output_file, "wb") as audio_file:
                audio_file.write(audio_content)
        else:
            print(f"Request failed with status code {response.status_code}")
            print(response.json())
import os
import uuid
import json
import base64
import requests
from config.logger import setup_logging
from datetime import datetime
from core.providers.tts.base import TTSProviderBase
TAG = __name__
logger = setup_logging()
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.url = config.get("url")
        self.text_lang = config.get("text_lang", "zh")
        self.ref_audio_path = config.get("ref_audio_path")
        self.prompt_text = config.get("prompt_text")
        self.prompt_lang = config.get("prompt_lang", "zh")
        self.top_k = int(config.get("top_k", 5))
        self.top_p = float(config.get("top_p", 1))
        self.temperature = float(config.get("temperature", 1))
        self.text_split_method = config.get("text_split_method", "cut0")
        self.batch_size = int(config.get("batch_size", 1))
        self.batch_threshold = float(config.get("batch_threshold", 0.75))
        self.split_bucket = bool(config.get("split_bucket", True))
        self.return_fragment = bool(config.get("return_fragment", False))
        self.speed_factor = float(config.get("speed_factor", 1.0))
        self.streaming_mode = bool(config.get("streaming_mode", False))
        self.seed = int(config.get("seed", -1))
        self.parallel_infer = bool(config.get("parallel_infer", True))
        self.repetition_penalty = float(config.get("repetition_penalty", 1.35))
        self.aux_ref_audio_paths = config.get("aux_ref_audio_paths", [])
    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )
    async def text_to_speak(self, text, output_file):
        request_json = {
            "text": text,
            "text_lang": self.text_lang,
            "ref_audio_path": self.ref_audio_path,
            "aux_ref_audio_paths": self.aux_ref_audio_paths,
            "prompt_text": self.prompt_text,
            "prompt_lang": self.prompt_lang,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "temperature": self.temperature,
            "text_split_method": self.text_split_method,
            "batch_size": self.batch_size,
            "batch_threshold": self.batch_threshold,
            "split_bucket": self.split_bucket,
            "return_fragment": self.return_fragment,
            "speed_factor": self.speed_factor,
            "streaming_mode": self.streaming_mode,
            "seed": self.seed,
            "parallel_infer": self.parallel_infer,
            "repetition_penalty": self.repetition_penalty,
        }
        resp = requests.post(self.url, json=request_json)
        if resp.status_code == 200:
            with open(output_file, "wb") as file:
                file.write(resp.content)
        else:
            logger.bind(tag=TAG).error(
                f"GPT_SoVITS_V2 TTS请求失败: {resp.status_code} - {resp.text}"
            )
import os
import uuid
import requests
from config.logger import setup_logging
from datetime import datetime
from core.providers.tts.base import TTSProviderBase
TAG = __name__
logger = setup_logging()
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.url = config.get("url")
        self.refer_wav_path = config.get("refer_wav_path")
        self.prompt_text = config.get("prompt_text")
        self.prompt_language = config.get("prompt_language")
        self.text_language = config.get("text_language", "audo")
        self.top_k = int(config.get("top_k", 15))
        self.top_p = float(config.get("top_p", 1.0))
        self.temperature = float(config.get("temperature", 1.0))
        self.cut_punc = config.get("cut_punc", "")
        self.speed = float(config.get("speed", 1.0))
        self.inp_refs = config.get("inp_refs", [])
        self.sample_steps = int(config.get("sample_steps", 32))
        self.if_sr = bool(config.get("if_sr", False))
    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )
    async def text_to_speak(self, text, output_file):
        request_params = {
            "refer_wav_path": self.refer_wav_path,
            "prompt_text": self.prompt_text,
            "prompt_language": self.prompt_language,
            "text": text,
            "text_language": self.text_language,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "temperature": self.temperature,
            "cut_punc": self.cut_punc,
            "speed": self.speed,
            "inp_refs": self.inp_refs,
            "sample_steps": self.sample_steps,
            "if_sr": self.if_sr,
        }
        resp = requests.get(self.url, params=request_params)
        if resp.status_code == 200:
            with open(output_file, "wb") as file:
                file.write(resp.content)
        else:
            logger.bind(tag=TAG).error(
                f"GPT_SoVITS_V3 TTS请求失败: {resp.status_code} - {resp.text}"
            )
import os
import uuid
import json
import requests
from datetime import datetime
from core.providers.tts.base import TTSProviderBase
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.group_id = config.get("group_id")
        self.api_key = config.get("api_key")
        self.model = config.get("model")
        if config.get("private_voice"):
            self.voice_id = config.get("private_voice")
        else:
            self.voice_id = config.get("voice_id")
        default_voice_setting = {
            "voice_id": "female-shaonv",
            "speed": 1,
            "vol": 1,
            "pitch": 0,
            "emotion": "happy",
        }
        default_pronunciation_dict = {"tone": ["处理/(chu3)(li3)", "危险/dangerous"]}
        defult_audio_setting = {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        }
        self.voice_setting = {
            **default_voice_setting,
            **config.get("voice_setting", {}),
        }
        self.pronunciation_dict = {
            **default_pronunciation_dict,
            **config.get("pronunciation_dict", {}),
        }
        self.audio_setting = {**defult_audio_setting, **config.get("audio_setting", {})}
        self.timber_weights = config.get("timber_weights", [])
        if self.voice_id:
            self.voice_setting["voice_id"] = self.voice_id
        self.host = "api.minimax.chat"
        self.api_url = f"https://{self.host}/v1/t2a_v2?GroupId={self.group_id}"
        self.header = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
    def generate_filename(self, extension=".mp3"):
        return os.path.join(
            self.output_file,
            f"tts-{__name__}{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )
    async def text_to_speak(self, text, output_file):
        request_json = {
            "model": self.model,
            "text": text,
            "stream": False,
            "voice_setting": self.voice_setting,
            "pronunciation_dict": self.pronunciation_dict,
            "audio_setting": self.audio_setting,
        }
        if type(self.timber_weights) is list and len(self.timber_weights) > 0:
            request_json["timber_weights"] = self.timber_weights
            request_json["voice_setting"]["voice_id"] = ""
        try:
            resp = requests.post(
                self.api_url, json.dumps(request_json), headers=self.header
            )
            # 检查返回请求数据的status_code是否为0
            if resp.json()["base_resp"]["status_code"] == 0:
                data = resp.json()["data"]["audio"]
                file_to_save = open(output_file, "wb")
                file_to_save.write(bytes.fromhex(data))
            else:
                raise Exception(
                    f"{__name__} status_code: {resp.status_code} response: {resp.content}"
                )
        except Exception as e:
            raise Exception(f"{__name__} error: {e}")
import os
import uuid
import requests
from datetime import datetime
from core.utils.util import check_model_key
from core.providers.tts.base import TTSProviderBase
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.api_key = config.get("api_key")
        self.api_url = config.get("api_url", "https://api.openai.com/v1/audio/speech")
        self.model = config.get("model", "tts-1")
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice", "alloy")
        self.response_format = "wav"
        self.speed = float(config.get("speed", 1.0))
        self.output_file = config.get("output_dir", "tmp/")
        check_model_key("TTS", self.api_key)
    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )
    async def text_to_speak(self, text, output_file):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        data = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "wav",
            "speed": self.speed,
        }
        response = requests.post(self.api_url, json=data, headers=headers)
        if response.status_code == 200:
            with open(output_file, "wb") as audio_file:
                audio_file.write(response.content)
        else:
            raise Exception(
                f"OpenAI TTS请求失败: {response.status_code} - {response.text}"
            )
import os
import uuid
import requests
from datetime import datetime
from core.providers.tts.base import TTSProviderBase
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.model = config.get("model")
        self.access_token = config.get("access_token")
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice")
        self.response_format = config.get("response_format")
        self.sample_rate = config.get("sample_rate")
        self.speed = float(config.get("speed", 1.0))
        self.gain = config.get("gain")
        self.host = "api.siliconflow.cn"
        self.api_url = f"https://{self.host}/v1/audio/speech"
    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )
    async def text_to_speak(self, text, output_file):
        request_json = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": self.response_format,
        }
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        response = requests.request(
            "POST", self.api_url, json=request_json, headers=headers
        )
        data = response.content
        file_to_save = open(output_file, "wb")
        file_to_save.write(data)
import hashlib
import hmac
import os
import time
import uuid
import json
import base64
import requests
from datetime import datetime, timezone
from core.providers.tts.base import TTSProviderBase
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.appid = config.get("appid")
        self.secret_id = config.get("secret_id")
        self.secret_key = config.get("secret_key")
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = int(config.get("voice"))
        self.api_url = "https://tts.tencentcloudapi.com"  # 正确的API端点
        self.region = config.get("region")
        self.output_file = config.get("output_dir")
    def _get_auth_headers(self, request_body):
        """生成鉴权请求头"""
        # 获取当前UTC时间戳
        timestamp = int(time.time())
        # 使用UTC时间计算日期
        utc_date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        # 服务名称必须是 "tts"
        service = "tts"
        # 拼接凭证范围
        credential_scope = f"{utc_date}/{service}/tc3_request"
        # 使用TC3-HMAC-SHA256签名方法
        algorithm = "TC3-HMAC-SHA256"
        # 构建规范请求字符串
        http_request_method = "POST"
        canonical_uri = "/"
        canonical_querystring = ""
        # 请求头必须包含host和content-type，且按字典序排列
        canonical_headers = (
            f"content-type:application/json\n" f"host:tts.tencentcloudapi.com\n"
        )
        signed_headers = "content-type;host"
        # 请求体哈希值
        payload = json.dumps(request_body)
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        # 构建规范请求字符串
        canonical_request = (
            f"{http_request_method}\n"
            f"{canonical_uri}\n"
            f"{canonical_querystring}\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )
        # 计算规范请求的哈希值
        hashed_canonical_request = hashlib.sha256(
            canonical_request.encode("utf-8")
        ).hexdigest()
        # 构建待签名字符串
        string_to_sign = (
            f"{algorithm}\n"
            f"{timestamp}\n"
            f"{credential_scope}\n"
            f"{hashed_canonical_request}"
        )
        # 计算签名密钥
        secret_date = self._hmac_sha256(
            f"TC3{self.secret_key}".encode("utf-8"), utc_date
        )
        secret_service = self._hmac_sha256(secret_date, service)
        secret_signing = self._hmac_sha256(secret_service, "tc3_request")
        # 计算签名
        signature = hmac.new(
            secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        # 构建授权头
        authorization = (
            f"{algorithm} "
            f"Credential={self.secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        # 构建请求头
        headers = {
            "Content-Type": "application/json",
            "Host": "tts.tencentcloudapi.com",
            "Authorization": authorization,
            "X-TC-Action": "TextToVoice",
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": "2019-08-23",
            "X-TC-Region": self.region,
            "X-TC-Language": "zh-CN",
        }
        return headers
    def _hmac_sha256(self, key, msg):
        """HMAC-SHA256加密"""
        if isinstance(msg, str):
            msg = msg.encode("utf-8")
        return hmac.new(key, msg, hashlib.sha256).digest()
    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )
    async def text_to_speak(self, text, output_file):
        # 构建请求体
        request_json = {
            "Text": text,  # 合成语音的源文本
            "SessionId": str(uuid.uuid4()),  # 会话ID，随机生成
            "VoiceType": int(self.voice),  # 音色
        }
        try:
            # 获取请求头（每次请求都重新生成，以确保时间戳和签名是最新的）
            headers = self._get_auth_headers(request_json)
            # 发送请求
            resp = requests.post(
                self.api_url, json.dumps(request_json), headers=headers
            )
            # 检查响应
            if resp.status_code == 200:
                response_data = resp.json()
                # 检查是否成功
                if response_data.get("Response", {}).get("Error") is not None:
                    error_info = response_data["Response"]["Error"]
                    raise Exception(
                        f"API返回错误: {error_info['Code']}: {error_info['Message']}"
                    )
                # 提取音频数据
                audio_data = response_data["Response"].get("Audio")
                if audio_data:
                    # 解码Base64音频数据并保存
                    with open(output_file, "wb") as f:
                        f.write(base64.b64decode(audio_data))
                else:
                    raise Exception(f"{__name__}: 没有返回音频数据: {response_data}")
            else:
                raise Exception(
                    f"{__name__} status_code: {resp.status_code} response: {resp.content}"
                )
        except Exception as e:
            raise Exception(f"{__name__} error: {e}")
import os
import uuid
import json
import requests
import shutil
from datetime import datetime
from core.providers.tts.base import TTSProviderBase
class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.url = config.get(
            "url",
            "https://u95167-bd74-2aef8085.westx.seetacloud.com:8443/flashsummary/tts?token=",
        )
        if config.get("private_voice"):
            self.voice_id = int(config.get("private_voice"))
        else:
            self.voice_id = int(config.get("voice_id", 1695))
        self.token = config.get("token")
        self.to_lang = config.get("to_lang")
        self.volume_change_dB = int(config.get("volume_change_dB", 0))
        self.speed_factor = int(config.get("speed_factor", 1))
        self.stream = bool(config.get("stream", False))
        self.output_file = config.get("output_dir")
        self.pitch_factor = int(config.get("pitch_factor", 0))
        self.format = config.get("format", "mp3")
        self.emotion = int(config.get("emotion", 1))
        self.header = {"Content-Type": "application/json"}
    def generate_filename(self, extension=".mp3"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )
    async def text_to_speak(self, text, output_file):
        url = f"{self.url}{self.token}"
        result = "firefly"
        payload = json.dumps(
            {
                "to_lang": self.to_lang,
                "text": text,
                "emotion": self.emotion,
                "format": self.format,
                "volume_change_dB": self.volume_change_dB,
                "voice_id": self.voice_id,
                "pitch_factor": self.pitch_factor,
                "speed_factor": self.speed_factor,
                "token": self.token,
            }
        )
        resp = requests.request("POST", url, data=payload)
        if resp.status_code != 200:
            return None
        resp_json = resp.json()
        try:
            result = (
                resp_json["url"]
                + ":"
                + str(resp_json["port"])
                + "/flashsummary/retrieveFileData?stream=True&token="
                + self.token
                + "&voice_audio_path="
                + resp_json["voice_path"]
            )
        except Exception as e:
            print("error:", e)
        audio_content = requests.get(result)
        with open(output_file, "wb") as f:
            f.write(audio_content.content)
            return True
        voice_path = resp_json.get("voice_path")
        des_path = output_file
        shutil.move(voice_path, des_path)
from abc import ABC, abstractmethod
from typing import Optional
class VADProviderBase(ABC):
    @abstractmethod
    def is_vad(self, conn, data) -> bool:
        """检测音频数据中的语音活动"""
        pass
import time
import numpy as np
import torch
import opuslib_next
from config.logger import setup_logging
from core.providers.vad.base import VADProviderBase
TAG = __name__
logger = setup_logging()
class VADProvider(VADProviderBase):
    def __init__(self, config):
        logger.bind(tag=TAG).info("SileroVAD", config)
        self.model, self.utils = torch.hub.load(
            repo_or_dir=config["model_dir"],
            source="local",
            model="silero_vad",
            force_reload=False,
        )
        (get_speech_timestamps, _, _, _, _) = self.utils
        self.decoder = opuslib_next.Decoder(16000, 1)
        self.vad_threshold = float(config.get("threshold", 0.5))
        self.silence_threshold_ms = int(config.get("min_silence_duration_ms", 1000))
    def is_vad(self, conn, opus_packet):
        try:
            pcm_frame = self.decoder.decode(opus_packet, 960)
            conn.client_audio_buffer.extend(pcm_frame)  # 将新数据加入缓冲区
            # 处理缓冲区中的完整帧（每次处理512采样点）
            client_have_voice = False
            while len(conn.client_audio_buffer) >= 512 * 2:
                # 提取前512个采样点（1024字节）
                chunk = conn.client_audio_buffer[: 512 * 2]
                conn.client_audio_buffer = conn.client_audio_buffer[512 * 2 :]
                # 转换为模型需要的张量格式
                audio_int16 = np.frombuffer(chunk, dtype=np.int16)
                audio_float32 = audio_int16.astype(np.float32) / 32768.0
                audio_tensor = torch.from_numpy(audio_float32)
                # 检测语音活动
                with torch.no_grad():
                    speech_prob = self.model(audio_tensor, 16000).item()
                client_have_voice = speech_prob >= self.vad_threshold
                # 如果之前有声音，但本次没有声音，且与上次有声音的时间查已经超过了静默阈值，则认为已经说完一句话
                if conn.client_have_voice and not client_have_voice:
                    stop_duration = (
                        time.time() * 1000 - conn.client_have_voice_last_time
                    )
                    if stop_duration >= self.silence_threshold_ms:
                        conn.client_voice_stop = True
                if client_have_voice:
                    conn.client_have_voice = True
                    conn.client_have_voice_last_time = time.time() * 1000
            return client_have_voice
        except opuslib_next.OpusError as e:
            logger.bind(tag=TAG).info(f"解码错误: {e}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error processing audio packet: {e}")
import importlib
import logging
import os
import sys
import time
import wave
import uuid
from abc import ABC, abstractmethod
from typing import Optional, Tuple, List
from core.providers.asr.base import ASRProviderBase
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
def create_instance(class_name: str, *args, **kwargs) -> ASRProviderBase:
    """工厂方法创建ASR实例"""
    if os.path.exists(os.path.join('core', 'providers', 'asr', f'{class_name}.py')):
        lib_name = f'core.providers.asr.{class_name}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
        return sys.modules[lib_name].ASRProvider(*args, **kwargs)
    raise ValueError(f"不支持的ASR类型: {class_name}，请检查该配置的type是否设置正确")
import uuid
from typing import List, Dict
from datetime import datetime
class Message:
    def __init__(self, role: str, content: str = None, uniq_id: str = None, tool_calls = None, tool_call_id=None):
        self.uniq_id = uniq_id if uniq_id is not None else str(uuid.uuid4())
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
class Dialogue:
    def __init__(self):
        self.dialogue: List[Message] = []
        # 获取当前时间
        self.current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    def put(self, message: Message):
        self.dialogue.append(message)
    def getMessages(self, m, dialogue):
        if m.tool_calls is not None:
            dialogue.append({"role": m.role, "tool_calls": m.tool_calls})
        elif m.role == "tool":
            dialogue.append({"role": m.role, "tool_call_id": m.tool_call_id, "content": m.content})
        else:
            dialogue.append({"role": m.role, "content": m.content})
    def get_llm_dialogue(self) -> List[Dict[str, str]]:
        dialogue = []
        for m in self.dialogue:
            self.getMessages(m, dialogue)
        return dialogue
    def update_system_message(self, new_content: str):
        """更新或添加系统消息"""
        # 查找第一个系统消息
        system_msg = next((msg for msg in self.dialogue if msg.role == "system"), None)
        if system_msg:
            system_msg.content = new_content
        else:
            self.put(Message(role="system", content=new_content))
    def get_llm_dialogue_with_memory(self, memory_str: str = None) -> List[Dict[str, str]]:
        if memory_str is None or len(memory_str) == 0:
            return self.get_llm_dialogue()
        # 构建带记忆的对话
        dialogue = []
        # 添加系统提示和记忆
        system_message = next(
            (msg for msg in self.dialogue if msg.role == "system"), None
        )
        if system_message:
            enhanced_system_prompt = (
                f"{system_message.content}\n\n"
                f"相关记忆：\n{memory_str}"
            )
            dialogue.append({"role": "system", "content": enhanced_system_prompt})
        # 添加用户和助手的对话
        for m in self.dialogue:
            if m.role != "system":  # 跳过原始的系统消息
                self.getMessages(m, dialogue)
        return dialogue
import os
import sys
from config.logger import setup_logging
import importlib
logger = setup_logging()
def create_instance(class_name, *args, **kwargs):
    # 创建intent实例
    if os.path.exists(os.path.join('core', 'providers', 'intent', class_name, f'{class_name}.py')):
        lib_name = f'core.providers.intent.{class_name}.{class_name}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
        return sys.modules[lib_name].IntentProvider(*args, **kwargs)
    raise ValueError(f"不支持的intent类型: {class_name}，请检查该配置的type是否设置正确")
import os
import sys
# 添加项目根目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
sys.path.insert(0, project_root)
from config.logger import setup_logging
import importlib
logger = setup_logging()
def create_instance(class_name, *args, **kwargs):
    # 创建LLM实例
    if os.path.exists(os.path.join('core', 'providers', 'llm', class_name, f'{class_name}.py')):
        lib_name = f'core.providers.llm.{class_name}.{class_name}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
        return sys.modules[lib_name].LLMProvider(*args, **kwargs)
    raise ValueError(f"不支持的LLM类型: {class_name}，请检查该配置的type是否设置正确")
import os
import sys
import importlib
from config.logger import setup_logging
logger = setup_logging()
def create_instance(class_name, *args, **kwargs):
    if os.path.exists(
        os.path.join("core", "providers", "memory", class_name, f"{class_name}.py")
    ):
        lib_name = f"core.providers.memory.{class_name}.{class_name}"
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f"{lib_name}")
        return sys.modules[lib_name].MemoryProvider(*args, **kwargs)
    raise ValueError(f"不支持的记忆服务类型: {class_name}")
import struct
def decode_opus_from_file(input_file):
    """
    从p3文件中解码 Opus 数据，并返回一个 Opus 数据包的列表以及总时长。
    """
    opus_datas = []
    total_frames = 0
    sample_rate = 16000  # 文件采样率
    frame_duration_ms = 60  # 帧时长
    frame_size = int(sample_rate * frame_duration_ms / 1000)
    with open(input_file, 'rb') as f:
        while True:
            # 读取头部（4字节）：[1字节类型，1字节保留，2字节长度]
            header = f.read(4)
            if not header:
                break
            # 解包头部信息
            _, _, data_len = struct.unpack('>BBH', header)
            # 根据头部指定的长度读取 Opus 数据
            opus_data = f.read(data_len)
            if len(opus_data) != data_len:
                raise ValueError(f"Data length({len(opus_data)}) mismatch({data_len}) in the file.")
            opus_datas.append(opus_data)
            total_frames += 1
    # 计算总时长
    total_duration = (total_frames * frame_duration_ms) / 1000.0
    return opus_datas, total_duration
import os
import re
import sys
from config.logger import setup_logging
import importlib
logger = setup_logging()
def create_instance(class_name, *args, **kwargs):
    # 创建TTS实例
    if os.path.exists(os.path.join('core', 'providers', 'tts', f'{class_name}.py')):
        lib_name = f'core.providers.tts.{class_name}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
        return sys.modules[lib_name].TTSProvider(*args, **kwargs)
    raise ValueError(f"不支持的TTS类型: {class_name}，请检查该配置的type是否设置正确")
class MarkdownCleaner:
    """
    封装 Markdown 清理逻辑：直接用 MarkdownCleaner.clean_markdown(text) 即可
    """
    # 公式字符
    NORMAL_FORMULA_CHARS = re.compile(r'[a-zA-Z\\^_{}\+\-\(\)\[\]=]')
    @staticmethod
    def _replace_inline_dollar(m: re.Match) -> str:
        """
        只要捕获到完整的 "$...$":
          - 如果内部有典型公式字符 => 去掉两侧 $
          - 否则 (纯数字/货币等) => 保留 "$...$"
        """
        content = m.group(1)
        if MarkdownCleaner.NORMAL_FORMULA_CHARS.search(content):
            return content
        else:
            return m.group(0)
    @staticmethod
    def _replace_table_block(match: re.Match) -> str:
        """
        当匹配到一个整段表格块时，回调该函数。
        """
        block_text = match.group('table_block')
        lines = block_text.strip('\n').split('\n')
        parsed_table = []
        for line in lines:
            line_stripped = line.strip()
            if re.match(r'^\|\s*[-:]+\s*(\|\s*[-:]+\s*)+\|?$', line_stripped):
                continue
            columns = [col.strip() for col in line_stripped.split('|') if col.strip() != '']
            if columns:
                parsed_table.append(columns)
        if not parsed_table:
            return ""
        headers = parsed_table[0]
        data_rows = parsed_table[1:] if len(parsed_table) > 1 else []
        lines_for_tts = []
        if len(parsed_table) == 1:
            # 只有一行
            only_line_str = ", ".join(parsed_table[0])
            lines_for_tts.append(f"单行表格：{only_line_str}")
        else:
            lines_for_tts.append(f"表头是：{', '.join(headers)}")
            for i, row in enumerate(data_rows, start=1):
                row_str_list = []
                for col_index, cell_val in enumerate(row):
                    if col_index < len(headers):
                        row_str_list.append(f"{headers[col_index]} = {cell_val}")
                    else:
                        row_str_list.append(cell_val)
                lines_for_tts.append(f"第 {i} 行：{', '.join(row_str_list)}")
        return "\n".join(lines_for_tts) + "\n"
    # 预编译所有正则表达式（按执行频率排序）
    # 这里要把 replace_xxx 的静态方法放在最前定义，以便在列表里能正确引用它们。
    REGEXES = [
        (re.compile(r'```.*?```', re.DOTALL), ''),  # 代码块
        (re.compile(r'^#+\s*', re.MULTILINE), ''),  # 标题
        (re.compile(r'(\*\*|__)(.*?)\1'), r'\2'),  # 粗体
        (re.compile(r'(\*|_)(?=\S)(.*?)(?<=\S)\1'), r'\2'),  # 斜体
        (re.compile(r'!\[.*?\]\(.*?\)'), ''),  # 图片
        (re.compile(r'\[(.*?)\]\(.*?\)'), r'\1'),  # 链接
        (re.compile(r'^\s*>+\s*', re.MULTILINE), ''),  # 引用
        (
            re.compile(r'(?P<table_block>(?:^[^\n]*\|[^\n]*\n)+)', re.MULTILINE),
            _replace_table_block
        ),
        (re.compile(r'^\s*[*+-]\s*', re.MULTILINE), '- '),  # 列表
        (re.compile(r'\$\$.*?\$\$', re.DOTALL), ''),  # 块级公式
        (
            re.compile(r'(?<![A-Za-z0-9])\$([^\n$]+)\$(?![A-Za-z0-9])'),
            _replace_inline_dollar
        ),
        (re.compile(r'\n{2,}'), '\n'),  # 多余空行
    ]
    @staticmethod
    def clean_markdown(text: str) -> str:
        """
        主入口方法：依序执行所有正则，移除或替换 Markdown 元素
        """
        for regex, replacement in MarkdownCleaner.REGEXES:
            text = regex.sub(replacement, text)
        return text.strip()
import json
import socket
import subprocess
import re
import requests
from typing import Dict, Any
from core.utils import tts, llm, intent, memory, vad, asr
TAG = __name__
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Connect to Google's DNS servers
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception as e:
        return "127.0.0.1"
def is_private_ip(ip_addr):
    """
    Check if an IP address is a private IP address (compatible with IPv4 and IPv6).
    @param {string} ip_addr - The IP address to check.
    @return {bool} True if the IP address is private, False otherwise.
    """
    try:
        # Validate IPv4 or IPv6 address format
        if not re.match(
            r"^(\d{1,3}\.){3}\d{1,3}$|^([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}$", ip_addr
        ):
            return False  # Invalid IP address format
        # IPv4 private address ranges
        if "." in ip_addr:  # IPv4 address
            ip_parts = list(map(int, ip_addr.split(".")))
            if ip_parts[0] == 10:
                return True  # 10.0.0.0/8 range
            elif ip_parts[0] == 172 and 16 <= ip_parts[1] <= 31:
                return True  # 172.16.0.0/12 range
            elif ip_parts[0] == 192 and ip_parts[1] == 168:
                return True  # 192.168.0.0/16 range
            elif ip_addr == "127.0.0.1":
                return True  # Loopback address
            elif ip_parts[0] == 169 and ip_parts[1] == 254:
                return True  # Link-local address 169.254.0.0/16
            else:
                return False  # Not a private IPv4 address
        else:  # IPv6 address
            ip_addr = ip_addr.lower()
            if ip_addr.startswith("fc00:") or ip_addr.startswith("fd00:"):
                return True  # Unique Local Addresses (FC00::/7)
            elif ip_addr == "::1":
                return True  # Loopback address
            elif ip_addr.startswith("fe80:"):
                return True  # Link-local unicast addresses (FE80::/10)
            else:
                return False  # Not a private IPv6 address
    except (ValueError, IndexError):
        return False  # IP address format error or insufficient segments
def get_ip_info(ip_addr, logger):
    try:
        if is_private_ip(ip_addr):
            ip_addr = ""
        url = f"https://whois.pconline.com.cn/ipJson.jsp?json=true&ip={ip_addr}"
        resp = requests.get(url).json()
        ip_info = {"city": resp.get("city")}
        return ip_info
    except Exception as e:
        logger.bind(tag=TAG).error(f"Error getting client ip info: {e}")
        return {}
def write_json_file(file_path, data):
    """将数据写入 JSON 文件"""
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)
def is_punctuation_or_emoji(char):
    """检查字符是否为空格、指定标点或表情符号"""
    # 定义需要去除的中英文标点（包括全角/半角）
    punctuation_set = {
        "，",
        ",",  # 中文逗号 + 英文逗号
        "。",
        ".",  # 中文句号 + 英文句号
        "！",
        "!",  # 中文感叹号 + 英文感叹号
        "-",
        "－",  # 英文连字符 + 中文全角横线
        "、",  # 中文顿号
    }
    if char.isspace() or char in punctuation_set:
        return True
    # 检查表情符号（保留原有逻辑）
    code_point = ord(char)
    emoji_ranges = [
        (0x1F600, 0x1F64F),
        (0x1F300, 0x1F5FF),
        (0x1F680, 0x1F6FF),
        (0x1F900, 0x1F9FF),
        (0x1FA70, 0x1FAFF),
        (0x2600, 0x26FF),
        (0x2700, 0x27BF),
    ]
    return any(start <= code_point <= end for start, end in emoji_ranges)
def get_string_no_punctuation_or_emoji(s):
    """去除字符串首尾的空格、标点符号和表情符号"""
    chars = list(s)
    # 处理开头的字符
    start = 0
    while start < len(chars) and is_punctuation_or_emoji(chars[start]):
        start += 1
    # 处理结尾的字符
    end = len(chars) - 1
    while end >= start and is_punctuation_or_emoji(chars[end]):
        end -= 1
    return "".join(chars[start : end + 1])
def remove_punctuation_and_length(text):
    # 全角符号和半角符号的Unicode范围
    full_width_punctuations = (
        "！＂＃＄％＆＇（）＊＋，－。／：；＜＝＞？＠［＼］＾＿｀｛｜｝～"
    )
    half_width_punctuations = r'!"#$%&\'()*+,-./:;<=>?@[\]^_`{|}~'
    space = " "  # 半角空格
    full_width_space = "　"  # 全角空格
    # 去除全角和半角符号以及空格
    result = "".join(
        [
            char
            for char in text
            if char not in full_width_punctuations
            and char not in half_width_punctuations
            and char not in space
            and char not in full_width_space
        ]
    )
    if result == "Yeah":
        return 0, ""
    return len(result), result
def check_model_key(modelType, modelKey):
    if "你" in modelKey:
        raise ValueError(
            "你还没配置" + modelType + "的密钥，请检查一下所使用的LLM是否配置了密钥"
        )
        return False
    return True
def check_ffmpeg_installed():
    ffmpeg_installed = False
    try:
        # 执行ffmpeg -version命令，并捕获输出
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,  # 如果返回码非零则抛出异常
        )
        # 检查输出中是否包含版本信息（可选）
        output = result.stdout + result.stderr
        if "ffmpeg version" in output.lower():
            ffmpeg_installed = True
        return False
    except (subprocess.CalledProcessError, FileNotFoundError):
        # 命令执行失败或未找到
        ffmpeg_installed = False
    if not ffmpeg_installed:
        error_msg = "您的电脑还没正确安装ffmpeg\n"
        error_msg += "\n建议您：\n"
        error_msg += "1、按照项目的安装文档，正确进入conda环境\n"
        error_msg += "2、查阅安装文档，如何在conda环境中安装ffmpeg\n"
        raise ValueError(error_msg)
def extract_json_from_string(input_string):
    """提取字符串中的 JSON 部分"""
    pattern = r"(\{.*\})"
    match = re.search(pattern, input_string)
    if match:
        return match.group(1)  # 返回提取的 JSON 字符串
    return None
def initialize_modules(
    logger,
    config: Dict[str, Any],
    init_vad=False,
    init_asr=False,
    init_llm=False,
    init_tts=False,
    init_memory=False,
    init_intent=False,
) -> Dict[str, Any]:
    """
    初始化所有模块组件
    Args:
        config: 配置字典
    Returns:
        Dict[str, Any]: 包含所有初始化后的模块的字典
    """
    modules = {}
    # 初始化TTS模块
    if init_tts:
        select_tts_module = config["selected_module"]["TTS"]
        tts_type = (
            select_tts_module
            if "type" not in config["TTS"][select_tts_module]
            else config["TTS"][select_tts_module]["type"]
        )
        modules["tts"] = tts.create_instance(
            tts_type,
            config["TTS"][select_tts_module],
            bool(config.get("delete_audio", True)),
        )
        logger.bind(tag=TAG).info(f"初始化组件: tts成功 {select_tts_module}")
    # 初始化LLM模块
    if init_llm:
        select_llm_module = config["selected_module"]["LLM"]
        llm_type = (
            select_llm_module
            if "type" not in config["LLM"][select_llm_module]
            else config["LLM"][select_llm_module]["type"]
        )
        modules["llm"] = llm.create_instance(
            llm_type,
            config["LLM"][select_llm_module],
        )
        logger.bind(tag=TAG).info(f"初始化组件: llm成功 {select_llm_module}")
    # 初始化Intent模块
    if init_intent:
        select_intent_module = config["selected_module"]["Intent"]
        intent_type = (
            select_intent_module
            if "type" not in config["Intent"][select_intent_module]
            else config["Intent"][select_intent_module]["type"]
        )
        modules["intent"] = intent.create_instance(
            intent_type,
            config["Intent"][select_intent_module],
        )
        logger.bind(tag=TAG).info(f"初始化组件: intent成功 {select_intent_module}")
    # 初始化Memory模块
    if init_memory:
        select_memory_module = config["selected_module"]["Memory"]
        memory_type = (
            select_memory_module
            if "type" not in config["Memory"][select_memory_module]
            else config["Memory"][select_memory_module]["type"]
        )
        modules["memory"] = memory.create_instance(
            memory_type,
            config["Memory"][select_memory_module],
        )
        logger.bind(tag=TAG).info(f"初始化组件: memory成功 {select_memory_module}")
    # 初始化VAD模块
    if init_vad:
        select_vad_module = config["selected_module"]["VAD"]
        vad_type = (
            select_vad_module
            if "type" not in config["VAD"][select_vad_module]
            else config["VAD"][select_vad_module]["type"]
        )
        modules["vad"] = vad.create_instance(
            vad_type,
            config["VAD"][select_vad_module],
        )
        logger.bind(tag=TAG).info(f"初始化组件: vad成功 {select_vad_module}")
    # 初始化ASR模块
    if init_asr:
        select_asr_module = config["selected_module"]["ASR"]
        asr_type = (
            select_asr_module
            if "type" not in config["ASR"][select_asr_module]
            else config["ASR"][select_asr_module]["type"]
        )
        modules["asr"] = asr.create_instance(
            asr_type,
            config["ASR"][select_asr_module],
            bool(config.get("delete_audio", True)),
        )
        logger.bind(tag=TAG).info(f"初始化组件: asr成功 {select_asr_module}")
    # 初始化自定义prompt
    if config.get("prompt", None) is not None:
        modules["prompt"] = config["prompt"]
        logger.bind(tag=TAG).info(f"初始化组件: prompt成功 {modules['prompt'][:50]}...")
    return modules
import importlib
import os
import sys
from core.providers.vad.base import VADProviderBase
from config.logger import setup_logging
TAG = __name__
logger = setup_logging()
def create_instance(class_name: str, *args, **kwargs) -> VADProviderBase:
    """工厂方法创建VAD实例"""
    if os.path.exists(os.path.join("core", "providers", "vad", f"{class_name}.py")):
        lib_name = f"core.providers.vad.{class_name}"
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f"{lib_name}")
        return sys.modules[lib_name].VADProvider(*args, **kwargs)
    raise ValueError(f"不支持的VAD类型: {class_name}，请检查该配置的type是否设置正确")
import asyncio
import websockets
from config.logger import setup_logging
from core.connection import ConnectionHandler
from core.utils.util import get_local_ip, initialize_modules
TAG = __name__
class WebSocketServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        modules = initialize_modules(
            self.logger, self.config, True, True, True, True, True, True
        )
        self._vad = modules["vad"]
        self._asr = modules["asr"]
        self._tts = modules["tts"]
        self._llm = modules["llm"]
        self._intent = modules["intent"]
        self._memory = modules["memory"]
        self.active_connections = set()
    async def start(self):
        server_config = self.config["server"]
        host = server_config["ip"]
        port = int(server_config.get("port", 8000))
        self.logger.bind(tag=TAG).info(
            "Server is running at ws://{}:{}/xiaozhi/v1/", get_local_ip(), port
        )
        self.logger.bind(tag=TAG).info(
            "=======上面的地址是websocket协议地址，请勿用浏览器访问======="
        )
        self.logger.bind(tag=TAG).info(
            "如想测试websocket请用谷歌浏览器打开test目录下的test_page.html"
        )
        self.logger.bind(tag=TAG).info(
            "=============================================================\n"
        )
        async with websockets.serve(self._handle_connection, host, port):
            await asyncio.Future()
    async def _handle_connection(self, websocket):
        """处理新连接，每次创建独立的ConnectionHandler"""
        # 创建ConnectionHandler时传入当前server实例
        handler = ConnectionHandler(
            self.config,
            self._vad,
            self._asr,
            self._llm,
            self._tts,
            self._memory,
            self._intent,
        )
        self.active_connections.add(handler)
        try:
            await handler.handle_connection(websocket)
        finally:
            self.active_connections.discard(handler)
