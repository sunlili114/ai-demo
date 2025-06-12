# 导入必要的模块
import eventlet
eventlet.monkey_patch()  # 对标准库进行monkey patch以支持异步
import socket
import json
from openai import OpenAI  # DeepSeek API客户端
import os
from flask import Flask, Response, request, send_from_directory  # Flask web框架
from flask_cors import CORS  # 跨域支持
from flask_socketio import SocketIO, emit, disconnect  # WebSocket支持
import httpx
from dotenv import load_dotenv  # 环境变量加载
from pathlib import Path
import redis  # Redis数据库客户端
from pymongo import MongoClient  # MongoDB客户端
import logging  # 日志记录

################################################################################################
# 配置日志系统
# 设置日志级别为DEBUG，记录所有级别的日志
logging.basicConfig(level=logging.DEBUG)
# 创建logger实例用于记录应用日志
logger = logging.getLogger(__name__)

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

############################################################################ai 常见的markdown渲染

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
CORS(app, resources={r"/*": {"origins": "*"}})

# 确保在创建 SocketIO 实例之前进行 monkey patching
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='eventlet',
    logger=True,
    engineio_logger=True,
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=100 * 1024 * 1024,
    reconnection=True,
    reconnection_attempts=5,
    reconnection_delay=1000,
    reconnection_delay_max=5000,
    transports=['websocket', 'polling']
)

# 环境变量
env_path = Path(__file__).parent / '.env'
load_dotenv()

api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    raise ValueError("❌ 未读取到密钥，请检查.env文件位置和内容")

# Redis键前缀定义
REDIS_SESSION_PREFIX = "session:"  # 用于存储会话数据的键前缀
REDIS_CONTEXT_PREFIX = "context:"  # 用于存储对话上下文的键前缀

# 初始化数据库连接（可选）
try:
    # 尝试连接Redis
    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", 6379))
    redis_db = int(os.getenv("REDIS_DB", 0))
    redis_client = redis.Redis(host=redis_host, port=redis_port, db=redis_db)
    redis_client.ping()  # 测试连接
    logger.info("Redis连接成功")
except Exception as e:
    logger.warning(f"Redis连接失败: {str(e)}")
    redis_client = None

try:
    # 尝试连接MongoDB
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    mongo_db_name = os.getenv("MONGO_DB", "myapp")
    mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)  # 2秒超时
    mongo_client.server_info()  # 测试连接
    mongo_db = mongo_client[mongo_db_name]
    
    # MongoDB集合
    users_collection = mongo_db["users"]
    sessions_collection = mongo_db["sessions"]
    messages_collection = mongo_db["messages"]
    contexts_collection = mongo_db["contexts"]
    logger.info("MongoDB连接成功")
except Exception as e:
    logger.warning(f"MongoDB连接失败: {str(e)}")
    mongo_client = None
    mongo_db = None
    users_collection = None
    sessions_collection = None
    messages_collection = None
    contexts_collection = None

client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com"
)

# 移除静态文件路由
# @app.route('/static/<path:filename>')
# def static_files(filename):
#     return send_from_directory(app.root_path, filename)

@app.route('/', methods=['GET', 'POST'])
def run():
    """
    根路由处理函数
    GET请求: 返回前端index.html页面
    POST请求: 接收内容并返回确认响应
    """
    if request.method == 'GET':
        # 返回前端页面
        return send_from_directory(app.template_folder, 'index.html')
    gpt_content = request.form.get('content')
    return Response("POST received", status=200)

@app.route('/redis-data', methods=['GET'])
def get_redis_data():
    if not redis_client:
        return json.dumps({"error": "Redis未连接"}), 503
    try:
        keys = redis_client.keys()
        data = {key.decode(): redis_client.get(key).decode() for key in keys}
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

@app.route('/mongo-data', methods=['GET'])
def get_mongo_data():
    if not mongo_db:
        return json.dumps({"error": "MongoDB未连接"}), 503
    try:
        collections = mongo_db.list_collection_names()
        data = {}
        for collection_name in collections:
            collection = mongo_db[collection_name]
            data[collection_name] = list(collection.find({}, {'_id': 0}).limit(10))
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

@app.route('/redis-context', methods=['GET'])
def get_redis_context():
    if not redis_client:
        return json.dumps({"error": "Redis未连接"}), 503
    try:
        context_keys = redis_client.keys(f"{REDIS_CONTEXT_PREFIX}*")
        data = {key.decode(): redis_client.get(key).decode() for key in context_keys}
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

@socketio.on('connect')
def handle_connect():
    """
    WebSocket连接建立时的处理函数
    记录客户端连接信息并发送确认消息
    """
    logger.info(f'Client connected: {request.sid}')
    try:
        # 向客户端发送连接确认消息
        emit('connection_ack', {'status': 'connected'})
    except Exception as e:
        logger.error(f'Error in handle_connect: {str(e)}')

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f'Client disconnected: {request.sid}')
    try:
        if redis_client:
            session_id = request.sid
            context_key = f"{REDIS_CONTEXT_PREFIX}{session_id}"
            
            # Clean up Redis data with error handling
            if redis_client.exists(context_key):
                redis_client.delete(context_key)
                logger.info(f'Successfully cleaned up Redis resources for session: {session_id}')
        
        if sessions_collection:
            # Clean up MongoDB session data if needed
            sessions_collection.delete_one({'session_id': session_id})
        
    except Exception as e:
        logger.error(f'Error during disconnect cleanup: {str(e)}')
    finally:
        try:
            disconnect()
        except Exception as e:
            logger.error(f'Error during disconnect: {str(e)}')

@socketio.on('user_message')
def handle_user_message(data):
    try:
        logger.info(f'Received message from {request.sid}: {data.get("message", "")}')
        
        # 验证消息数据
        if not isinstance(data, dict):
            raise ValueError("Invalid message format")
            
        user_text = data.get('message', '').strip()
        if not user_text:
            raise ValueError("Empty message content")

        # 发送开始标记
        emit('bot_reply_start', {'status': 'start'}, room=request.sid)
        
        # 获取或初始化上下文
        session_id = request.sid
        context_key = f"{REDIS_CONTEXT_PREFIX}{session_id}"
        
        messages = [
            {"role": "system", "content": "You are a helpful assistant"}
        ]
        
        # 如果Redis可用，尝试加载之前的对话上下文
        if redis_client:
            try:
                existing_context = redis_client.get(context_key)
                if existing_context:
                    messages.extend(json.loads(existing_context))
                logger.debug(f"Loaded context for session {session_id}")
            except Exception as e:
                logger.error(f"Error loading context: {str(e)}")
            
        # 添加当前用户消息
        messages.append({"role": "user", "content": user_text})

        try:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                stream=True
            )

            full_reply = ""
            current_code_block = ""
            in_code_block = False
            code_fence_count = 0

            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    full_reply += content

                    # 处理代码块的特殊情况
                    if '```' in content:
                        code_fence_count += content.count('```')
                        in_code_block = (code_fence_count % 2 != 0)

                    if in_code_block:
                        current_code_block += content
                        if '```' in content and not in_code_block:  # 代码块结束
                            emit('bot_reply_stream', {
                                'content': current_code_block,
                                'is_markdown': True
                            }, room=request.sid)
                            current_code_block = ""
                    else:
                        if content.strip():
                            emit('bot_reply_stream', {
                                'content': content,
                                'is_markdown': True
                            }, room=request.sid)

            # 发送最后可能残留的代码块
            if current_code_block:
                emit('bot_reply_stream', {
                    'content': current_code_block,
                    'is_markdown': True
                }, room=request.sid)

            # 将AI的回复添加到上下文
            messages.append({"role": "assistant", "content": full_reply})
            
            # 发送完成标记
            emit('bot_reply_done', {
                'full_content': full_reply,
                'is_markdown': True
            }, room=request.sid)
            
            # 如果Redis可用，保存当前对话上下文
            if redis_client:
                try:
                    # 保存当前对话上下文到Redis，限制最多保留5轮对话
                    if len(messages) > 10:  # 5轮对话(每轮user+assistant)
                        messages = messages[-10:]
                    redis_client.set(context_key, json.dumps(messages), ex=86400)  # 保存1天
                    logger.debug(f"Saved context for session {session_id}")
                except Exception as e:
                    logger.error(f"Error saving context: {str(e)}")
            
        except Exception as e:
            logger.error(f'Error in API call: {str(e)}')
            emit('bot_reply_error', {
                'error': f'API调用错误: {str(e)}'
            }, room=request.sid)
                
    except Exception as e:
        logger.error(f'Error in handle_user_message: {str(e)}')
        emit('bot_reply_error', {
            'error': str(e)
        }, room=request.sid)

@socketio.on_error_default
def default_error_handler(e):
    logger.error(f'Socket error: {str(e)}')
    emit('socket_error', {'error': str(e)}, room=request.sid)

if __name__ == '__main__': 
    print(f"访问地址: http://localhost:5000")
    try:
        socketio.run(app, host='0.0.0.0', port=5000, debug=True)
    except Exception as e:
        logger.error(f'Server error: {str(e)}')