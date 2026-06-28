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
const debounce = new Map();
const DEBOUNCE_MS = 60_000;

function isDebounced(uid) {
  const last = debounce.get(uid);
  if (last && Date.now() - last < DEBOUNCE_MS) return true;
  debounce.set(uid, Date.now());
  return false;
}

// 每 10 分钟清理过期条目
setInterval(() => {
  const now = Date.now();
  for (const [uid, ts] of debounce) {
    if (now - ts > 600_000) debounce.delete(uid);
  }
}, 600_000);

// ─── 引导文本 ───
function buildGuide(up, idx, dsp) {
  return (
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
    `用户应对上传内容负责，本服务仅提供功能引导，不对文件内容负责。\n\n` +
    `🔗 快速开始`
  );
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

  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ─── 消息处理 ───
async function handleMessage(msg, token, up, idx, dsp) {
  const chat = msg?.chat;
  if (!chat || chat.type !== "private") return;
  const uid = msg?.from?.id;
  if (!uid || isDebounced(uid)) return;

  const text = msg?.text || "";
  try {
    if (text === "/start") {
      await sendMessage(chat.id, buildGuide(up, idx, dsp), buildKeyboard(up, idx, dsp), token);
    } else {
      await sendMessage(chat.id, "请发送 /start 获取说明", undefined, token);
    }
  } catch {}
}

// ─── Workers 入口 ───
export default {
  async fetch(req, env) {
    if (req.headers.get("content-type")?.includes("application/json")) {
      try {
        const body = await req.json();
        if (body?.message) {
          await handleMessage(
            body.message,
            env.BOT_TOKEN,
            env.UP_BOT,
            env.IDX_BOT,
            env.DSP_BOT
          );
        }
      } catch {}
    }
    return new Response("OK");
  },
};