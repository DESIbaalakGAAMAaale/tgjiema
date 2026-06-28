/**
 * Mfile Bot — 引导机器人（哑巴导航员）
 * 部署于 Cloudflare Workers，零成本、零运维。
 *
 * 行为：
 *  - /start → 完整引导 + 3 按钮
 *  - 其他消息 → "请发送 /start 获取说明"
 *  - 私聊隔离 + 60s 防抖（Cache API，免费无限量）
 *  - 静默异常，不暴露错误给用户
 *
 * 免费额度：
 *  - Workers 10 万请求/天 + 10ms CPU/请求
 *  - Cache API 免费无限量（用于防抖）
 *  - 预计日请求 < 1 万，远低于限额
 */

import { Bot, webhookCallback } from "grammy";

// ─── 环境变量 ───
const BOT_TOKEN = "<BOT_TOKEN>"; // 部署时通过 wrangler secret put BOT_TOKEN 设置
const UP = globalThis.UPLOAD_BOT_USERNAME ?? "your_upload_bot";
const IDX = globalThis.DECODER_BOT_USERNAME ?? "your_decoder_bot";
const DSP = globalThis.SENDER_BOT_USERNAME ?? "your_sender_bot";

// ─── 防抖：Cache API，60s TTL ───
const DEBOUNCE_TTL = 60;
const DEBOUNCE_PREFIX = "https://debounce.local/";

async function isDebounced(userId) {
  const cache = caches.default;
  const url = DEBOUNCE_PREFIX + userId;
  const cached = await cache.match(url);
  return cached !== undefined;
}

async function setDebounce(userId) {
  const cache = caches.default;
  const url = DEBOUNCE_PREFIX + userId;
  const res = new Response("1", {
    headers: { "Cache-Control": `max-age=${DEBOUNCE_TTL}` },
  });
  // 使用 ctx.waitUntil 避免阻塞响应
  return cache.put(url, res);
}

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

// ─── 创建 Bot ───
const bot = new Bot(BOT_TOKEN);

// 私聊 /start
bot.command("start", async (ctx) => {
  // 非私聊静默丢弃
  if (ctx.chat?.type !== "private") return;

  const userId = ctx.from?.id;
  if (!userId) return;

  // 60s 防抖
  if (await isDebounced(userId)) return;

  try {
    await ctx.reply(buildGuide(), {
      reply_markup: buildKeyboard(),
      disable_web_page_preview: true,
    });
    await setDebounce(userId);
  } catch {
    // 静默丢弃
  }
});

// 其他所有消息
bot.on("message", async (ctx) => {
  // 非私聊静默丢弃
  if (ctx.chat?.type !== "private") return;

  const userId = ctx.from?.id;
  if (!userId) return;

  // 60s 防抖
  if (await isDebounced(userId)) return;

  try {
    await ctx.reply("请发送 /start 获取说明");
    await setDebounce(userId);
  } catch {
    // 静默丢弃
  }
});

// ─── CF Workers 入口 ───
export default {
  async fetch(request, env, ctx) {
    // 注入环境变量
    globalThis.UPLOAD_BOT_USERNAME = env.UPLOAD_BOT_USERNAME ?? UP;
    globalThis.DECODER_BOT_USERNAME = env.DECODER_BOT_USERNAME ?? IDX;
    globalThis.SENDER_BOT_USERNAME = env.SENDER_BOT_USERNAME ?? DSP;

    const handler = webhookCallback(bot, "cloudflare-mod", {
      timeoutSeconds: 8,
    });
    return handler(request, env, ctx);
  },
};