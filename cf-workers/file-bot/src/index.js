/**
 * Mfile Bot — 引导机器人
 * 部署于 Cloudflare Workers，零成本、零 KV 依赖。
 *
 * 行为:
 *  /start → 完整引导 + 3 按钮
 *  其他消息 → "请发送 /start 获取说明"
 *  私聊隔离 + 内存防抖 + 静默异常
 *
 * 免费额度: Workers 10 万请求/天，日活 5 万以内零风险。
 */

import { Bot, webhookCallback } from "grammy";

const BOT_TOKEN = "<BOT_TOKEN>";  // 部署时通过 wrangler secret put BOT_TOKEN 设置
const UP = globalThis._UP ?? "your_upload_bot";
const IDX = globalThis._IDX ?? "your_decoder_bot";
const DSP = globalThis._DSP ?? "your_sender_bot";

// ─── 内存防抖: 利用 isolate 复用，零成本 ───
const _debounce = new Map();
const DEBOUNCE_MS = 60_000;

function isDebounced(userId) {
  const last = _debounce.get(userId);
  if (last && Date.now() - last < DEBOUNCE_MS) return true;
  _debounce.set(userId, Date.now());
  return false;
}

// 定期清理，防止 Map 无限增长
setInterval(() => {
  const now = Date.now();
  for (const [uid, ts] of _debounce) {
    if (now - ts > 600_000) _debounce.delete(uid);
  }
}, 600_000);

// ─── 引导文本 ───
function buildGuide() {
  return (
    `📁 Mfile — 使用指南\n\n` +
    `📤 上传文件\n` +
    `向 @${UP} 发送文件，**@${IDX} 收到后会自动回复您文件码**。\n` +
    `⚠️ 请先启动 @${IDX}（发送 /start），以免无法接收文件码。\n\n` +
    `🔍 解码\n` +
    `向 @${IDX} 发送文件码，**@${DSP} 随后会将文件发送给您**。\n` +
    `⚠️ 请先启动 @${DSP}（发送 /start），以免无法接收文件。\n\n` +
    `📥 接收文件\n` +
    `解码成功后，@${DSP} 会自动将文件发送给您。\n\n` +
    `⚠️ 免责声明\n` +
    `用户应对上传内容负责，本服务仅提供功能引导，不对文件内容负责。\n\n` +
    `🔗 快速开始`
  );
}

function buildKeyboard() {
  const buttons = [];
  if (UP) buttons.push({ text: "📤 上传文件", url: `https://t.me/${UP}` });
  if (IDX) buttons.push({ text: "🔍 解码接码", url: `https://t.me/${IDX}` });
  if (DSP) buttons.push({ text: "📥 接收文件", url: `https://t.me/${DSP}` });
  return buttons.length ? { inline_keyboard: [buttons] } : undefined;
}

// ─── Bot ───
const bot = new Bot(BOT_TOKEN);

bot.command("start", async (ctx) => {
  if (ctx.chat?.type !== "private") return;
  const uid = ctx.from?.id;
  if (!uid || isDebounced(uid)) return;
  try {
    await ctx.reply(buildGuide(), {
      reply_markup: buildKeyboard(),
      disable_web_page_preview: true,
    });
  } catch {}
});

bot.on("message", async (ctx) => {
  if (ctx.chat?.type !== "private") return;
  const uid = ctx.from?.id;
  if (!uid || isDebounced(uid)) return;
  try {
    await ctx.reply("请发送 /start 获取说明");
  } catch {}
});

// ─── Workers 入口 ───
export default {
  async fetch(request, env) {
    globalThis._UP = env.UPLOAD_BOT_USERNAME ?? UP;
    globalThis._IDX = env.DECODER_BOT_USERNAME ?? IDX;
    globalThis._DSP = env.SENDER_BOT_USERNAME ?? DSP;
    return webhookCallback(bot, "cloudflare-mod", { timeoutSeconds: 8 })(request, env);
  },
};