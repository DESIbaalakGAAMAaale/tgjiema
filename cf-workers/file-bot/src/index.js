/**
 * Mfile Bot — 引导机器人（零依赖纯 JS）
 * 直接在 Cloudflare Dashboard 粘贴部署。
 *
 * 行为:
 *  /start → 完整引导 + 3 按钮
 *  其他消息 → "请发送 /start 获取说明"
 *  私聊隔离 + 内存防抖 + 静默异常
 */

// ─── 内存防抖 ───
// 注意：CF Workers 是请求驱动、用完即焚的，多 isolate 下防抖可能"失效"，
// 同一用户 60s 内可能收到两次回复。这对导航 bot 是可接受的，无需引入 KV。
const debounce = new Map();
const DEBOUNCE_MS = 60_000;

function isDebounced(uid) {
  const last = debounce.get(uid);
  if (last && Date.now() - last < DEBOUNCE_MS) return true;
  debounce.set(uid, Date.now());
  return false;
}

// ─── 引导文本 ───
function buildGuide(up, idx, dsp, channelLink) {
  let guide =
    `📁 Mfile — 使用指南\n\n` +
    `📤 上传文件\n` +
    `向 @${up} 发送文件，@${idx} 收到后会自动回复文件码。\n` +
    `⚠️ 请先启动 @${idx}（发送 /start），以免无法接收文件码。\n\n` +
    `🔍 解码\n` +
    `向 @${idx} 发送文件码，@${dsp} 随后会将文件发送给您。\n` +
    `⚠️ 请先启动 @${dsp}（发送 /start），以免无法接收文件。\n\n` +
    `📥 接收文件\n` +
    `解码成功后，@${dsp} 会自动将文件发送给您。\n\n` +
    `⚠️ 免责声明\n` +
    `用户应对上传内容负责，本服务仅提供功能引导，不对文件内容负责。`;
  if (channelLink) {
    guide += `\n\n📢 官方频道: ${channelLink}`;
  }
  guide += `\n\n🔗 快速开始`;
  return guide;
}

function buildKeyboard(up, idx, dsp) {
  const buttons = [];
  if (up) buttons.push({ text: "📤 上传文件", url: `https://t.me/${up}` });
  if (idx) buttons.push({ text: "🔍 解码接码", url: `https://t.me/${idx}` });
  if (dsp) buttons.push({ text: "📥 接收文件", url: `https://t.me/${dsp}` });
  return buttons.length ? { inline_keyboard: [buttons] } : undefined;
}

// ─── Telegram API ───
async function sendMessage(chatId, text, replyMarkup, token) {
  const body = {
    chat_id: chatId,
    text,
    disable_web_page_preview: true,
  };
  if (replyMarkup) body.reply_markup = replyMarkup;

  const resp = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    console.error(`[FileBot] sendMessage failed: ${resp.status} ${await resp.text()}`);
  }
}

// ─── 消息处理 ───
async function handleMessage(msg, token, up, idx, dsp, channelLink) {
  const chat = msg?.chat;
  if (!chat || chat.type !== "private") return;
  const uid = msg?.from?.id;
  if (!uid || isDebounced(uid)) return;

  const text = msg?.text || "";
  try {
    if (text === "/start") {
      await sendMessage(chat.id, buildGuide(up, idx, dsp, channelLink), buildKeyboard(up, idx, dsp), token);
    } else {
      await sendMessage(chat.id, "请发送 /start 获取说明", undefined, token);
    }
  } catch {}
}

// ─── Workers 入口 ───

// R39 P2-3: 使用 Web Crypto HMAC 做恒定时间比较,防止时序攻击泄露 secret
// 原实现 headerToken !== secret 是普通字符串比较,首字节不同即返回,
// 攻击者可通过响应时间差逐字节爆破 secret。
// 改用 crypto.subtle.verify(HMAC) 做恒定时间布尔比较。
async function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length === 0 || b.length === 0) return false;
  const encoder = new TextEncoder();
  // 用固定 key 派生 HMAC(仅用于比较,不暴露 secret)
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode("tgjiema-constant-time-compare-v1"),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
  // 对 a 计算 HMAC 签名
  const sigA = await crypto.subtle.sign(
    "HMAC", key, encoder.encode(a),
  );
  // verify 检查 sigA 是否为 b 的 HMAC(恒定时间布尔返回)
  // a === b 时签名匹配 → true;a !== b 时签名不匹配 → false
  return crypto.subtle.verify(
    "HMAC", key, sigA, encoder.encode(b),
  );
}

export default {
  async fetch(req, env) {
    // 强制验证 webhook secret token，防止伪造更新
    // 需在 Cloudflare Dashboard → Workers → Settings → Variables 中设置 SECRET_TOKEN
    // 或通过 wrangler secret put SECRET_TOKEN 设置
    // 安全默认:未配置 SECRET_TOKEN 时拒绝所有请求,防止任意用户伪造 Telegram update
    const secret = env.SECRET_TOKEN;
    if (!secret) {
      console.error("[FileBot][FATAL] SECRET_TOKEN 未配置,拒绝所有请求以防 webhook 伪造。请在 Cloudflare Dashboard 设置 SECRET_TOKEN");
      return new Response("Service Unavailable: SECRET_TOKEN not configured", { status: 503 });
    }
    const headerToken = req.headers.get("X-Telegram-Bot-Api-Secret-Token");
    // R39 P2-3: 改用 Web Crypto HMAC 恒定时间比较,不再使用 headerToken !== secret
    // 长度差异本身不是秘密(secret 长度可预测),但仍先做长度短路避免无谓的 HMAC 计算
    if (!headerToken || headerToken.length !== secret.length) {
      return new Response("Forbidden", { status: 403 });
    }
    const ok = await constantTimeEqual(headerToken, secret);
    if (!ok) {
      return new Response("Forbidden", { status: 403 });
    }

    if (req.headers.get("content-type")?.includes("application/json")) {
      try {
        const body = await req.json();
        if (body?.message) {
          await handleMessage(
            body.message,
            env.BOT_TOKEN,
            env.UP_BOT,
            env.IDX_BOT,
            env.DSP_BOT,
            env.CHANNEL_LINK || ""
          );
        }
      } catch {}
    }
    return new Response("OK");
  },
};