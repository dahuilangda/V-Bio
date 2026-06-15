# 外部系统登录

对接方后端签发短期 HS256 JWT，浏览器携带 token 跳转到 V-Bio。

## 对接参数

V-Bio 管理员会提供以下参数：

| 参数 | 用途 |
| --- | --- |
| `client_id` | 写入 JWT header 的 `kid` |
| `secret` | HS256 签名密钥，只保存在对接方后端 |
| `issuer` | 写入 payload 的 `iss` |
| `audience` | 写入 payload 的 `aud` |
| `ttl` | token 最大有效期，单位秒 |

跳转地址：

```text
https://<vbio-host>/auth/jwt?token=<JWT>&next=/projects
```

## Token 格式

Header：

```json
{"alg":"HS256","typ":"JWT","kid":"<client_id>"}
```

Payload：

```json
{
  "iss": "navigation",
  "aud": "vbio",
  "sub": "external-user-id",
  "username": "zhangsan",
  "email": "zhangsan@example.com",
  "name": "Zhang San",
  "iat": 1710000000,
  "exp": 1710000300
}
```

| 字段 | 要求 |
| --- | --- |
| `kid` | 接入方 `client_id`，由 V-Bio 的 Integrations 页面生成 |
| `iss` | 接入方配置的 issuer |
| `aud` | 接入方配置的 audience |
| `sub` | 外部系统稳定用户 ID |
| `username` | V-Bio 用户名来源 |
| `email` | V-Bio 优先使用邮箱匹配账号 |
| `iat` / `exp` | Unix 秒；有效期不能超过接入方 TTL |

## Node.js 签名

```js
import crypto from 'node:crypto';

function b64url(value) {
  return Buffer.from(value).toString('base64url');
}

export function signVbioToken({ clientId, secret, user }) {
  const now = Math.floor(Date.now() / 1000);
  const header = b64url(JSON.stringify({ alg: 'HS256', typ: 'JWT', kid: clientId }));
  const payload = b64url(JSON.stringify({
    iss: 'navigation',
    aud: 'vbio',
    sub: String(user.id),
    username: user.username,
    email: user.email,
    name: user.name,
    iat: now,
    exp: now + 300
  }));
  const body = `${header}.${payload}`;
  const sig = crypto.createHmac('sha256', secret).update(body).digest('base64url');
  return `${body}.${sig}`;
}
```

## Python 签名

```python
import base64
import hashlib
import hmac
import json
import time


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def sign_vbio_token(client_id: str, secret: str, user: dict) -> str:
    now = int(time.time())
    header = {'alg': 'HS256', 'typ': 'JWT', 'kid': client_id}
    payload = {
        'iss': 'navigation',
        'aud': 'vbio',
        'sub': str(user['id']),
        'username': user['username'],
        'email': user['email'],
        'name': user.get('name', user['username']),
        'iat': now,
        'exp': now + 300,
    }
    head = b64url(json.dumps(header, separators=(',', ':')).encode())
    body = b64url(json.dumps(payload, separators=(',', ':')).encode())
    signing_input = f'{head}.{body}'.encode()
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f'{head}.{body}.{b64url(signature)}'
```

## 账号处理

V-Bio 完成签名、issuer、audience 和过期时间校验后：

1. 有 `email` 时按邮箱匹配账号。
2. 没有 `email` 时按 `username` 匹配账号。
3. 未匹配到账号时创建普通用户。
超级管理员配置在 `frontend/.env`：

```env
VITE_SUPER_ADMIN_USERNAMES=dahuilangda
VITE_SUPER_ADMIN_EMAILS=dahuilangda@hotmail.com
```
