"""安全工具（安全子系统）：GitHub Token 的加解密。

这个文件属于「安全子系统」，专门负责一件小事：把用户的 GitHub 个人访问令牌（Token）
安全地存进数据库、再安全地读出来。Token 相当于账号密码，明文落库非常危险，
所以这里用对称加密（Fernet）对它做「加密存储、解密使用」。

关键内容：
- _derive_demo_key(secret)：把一个字符串密钥派生成 Fernet 需要的 32 字节格式（仅本地开发用）。
- encrypt_token(token, secret)：把明文 Token 加密成一段乱码字符串。
- decrypt_token(encrypted, secret)：把乱码字符串还原成明文 Token；如果密钥不对/被篡改就返回 None。

和其他模块的配合：
- db/models.py 里的 User / Repository 有 github_token_encrypted 字段，存的就是这里加密后的结果；
- 真正调 GitHub API 前，再用 decrypt_token 解开。
可以把它理解成「给 Token 配了一把带锁的保险箱」：钥匙（secret）由 config 提供，
加密后别人拿到库也看不出原始 Token。."""

# 从 cryptography 库的 fernet 模块引入 Fernet（对称加密工具）和 InvalidToken（解密失败抛的异常）
from cryptography.fernet import Fernet, InvalidToken


# 内部函数：根据用户给的 secret 字符串，派生出一个 Fernet 能用的密钥字节串
def _derive_demo_key(secret: str) -> bytes:
    # Fernet 需要 urlsafe base64 编码的 32 字节密钥。
    # 这里的确定性本地开发派生方式适合课程项目，不适合生产环境。
    # 引入 base64 模块：用来做 base64 编码（把二进制转成可存储的字符串）
    import base64
    # 引入 hashlib 模块：用来做哈希（sha256），把任意长度的 secret 变成固定 32 字节
    import hashlib

    # 把 secret 转成 utf-8 字节 → sha256 哈希成 32 字节摘要 → 再做 urlsafe base64 编码，得到合规密钥
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


# 加密函数：传入明文 token 和 secret，返回加密后的字符串
def encrypt_token(token: str, secret: str) -> str:
    # 用派生出的密钥生成 Fernet 对象 → 把 token 编码成字节后加密 → 解密结果是字节，再解码回字符串返回
    return Fernet(_derive_demo_key(secret)).encrypt(token.encode("utf-8")).decode("utf-8")


# 解密函数：传入密文和 secret，成功返回明文，失败（密钥不对/密文损坏）返回 None
def decrypt_token(encrypted: str, secret: str) -> str | None:
    # 用 try 包住解密过程，因为密钥不匹配或密文被篡改会抛 InvalidToken 异常
    try:
        # 用密钥生成 Fernet → 把密文字符串编码成字节解密 → 解密出字节再解码成明文返回
        return Fernet(_derive_demo_key(secret)).decrypt(encrypted.encode("utf-8")).decode("utf-8")
    # 捕获「令牌无效」异常：说明要么密钥错了，要么密文被动过
    except InvalidToken:
        # 不抛错，直接返回 None，让调用方决定怎么处理（比如提示用户重新授权）
        return None
