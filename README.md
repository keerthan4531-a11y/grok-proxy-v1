# Grok 4.6 OpenAI Proxy (Vercel Serverless Ready)

OpenAI-compatible reverse proxy for **Grok 4.6** directly from xAI, ready for 1-click deployment on **Vercel** with **ZERO tokens required**!

## ✨ Features

- ⚡ **100% Free & Anonymous**: Uses automated secp256k1 crypto keys to connect to `grok.com:443` via gRPC. No API key, no session token, and no login required!
- 🔄 **Auto-Healing & Key Rotation**: Automatically generates fresh keypairs on rate limits.
- 🌊 **OpenAI SSE Streaming**: Full real-time token streaming (`stream: true`).
- 🚀 **1-Click Deploy to Vercel**: Pre-configured with `vercel.json` and `api/index.py`.

## 📦 Deploy to Vercel

1. Push this repository to your GitHub account (`grok-proxy`).
2. Go to [Vercel Dashboard](https://vercel.com/new) -> Import `grok-proxy`.
3. Click **Deploy**!
4. Your OpenAI-compatible endpoint will be live at:
   ```
   https://your-grok-proxy.vercel.app/v1/chat/completions
   ```

## 🛠 Supported Models

- `grok-4.6` (Flagship Grok 4.6)
- `grok-4.6-thinking` (Deep Reasoning)
- `auto`

## 📡 API Usage

```bash
curl https://your-grok-proxy.vercel.app/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4.6",
    "messages": [{"role": "user", "content": "Hello Grok!"}],
    "stream": true
  }'
```
