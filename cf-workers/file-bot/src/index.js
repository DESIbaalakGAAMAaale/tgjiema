/**
 * Mfile Bot — 引导机器人(零依赖纯 JS)
 * 直接在 Cloudflare Dashboard 粘贴部署。
 *
 * 行为:
 *  /start → 完整引导 + 3 按钮
 *  其他消息 → "请发送 /start 获取说明"
 *  私聊隔离 + 静默异常
 *
 * R51 P1-8 整改:
 *  - 移除内存 debounce(多 isolate 不可靠,会重复回复)
 *  - 添加 Telegram update_id 持久去重(若绑定 UPDATE_ID_KV 则使用,否则跳过并注释)
 *  - 请求 body 大小上限 1MB + 基本 schema 校验
 *  - catch 块添加 console.warn 日志(仅 error.message,不含敏感信息)
 *  - 用户文案改为从顶部 locale 常量读取(中英文)
 *  - Telegram API 错误只记录 status code,不记录 body(可能含敏感信息)
 */

// ─── 用户文案 locale 常量(中英文) ───────────────────────────────
// R51 P1-8: 用户文案不再硬编码,统一从 LOCALE_MESSAGES 读取,
//            通过 detectLocale(msg) 选择语言(默认 zh-CN)
const LOCALE_MESSAGES = {
  "zh-CN": {
    guide_title: "📁 Mfile — 使用指南",
    upload_section: "📤 上传文件",
    upload_body: "向 @{up} 发送文件,@{idx} 收到后会自动回复文件码。",
    upload_warn: "⚠️ 请先启动 @{idx}(发送 /start),以免无法接收文件码。",
    decode_section: "🔍 解码",
    decode_body: "向 @{idx} 发送文件码,@{dsp} 随后会将文件发送给您。",
    decode_warn: "⚠️ 请先启动 @{dsp}(发送 /start),以免无法接收文件。",
    receive_section: "📥 接收文件",
    receive_body: "解码成功后,@{dsp} 会自动将文件发送给您。",
    disclaimer_section: "⚠️ 免责声明",
    disclaimer_body: "用户应对上传内容负责,本服务仅提供功能引导,不对文件内容负责。",
    channel_section: "📢 官方频道: {link}",
    quick_start: "🔗 快速开始",
    btn_upload: "📤 上传文件",
    btn_decode: "🔍 解码接码",
    btn_receive: "📥 接收文件",
    fallback_hint: "请发送 /start 获取说明",
    fatal_no_secret: "[FileBot][FATAL] SECRET_TOKEN 未配置,拒绝所有请求以防 webhook 伪造。请在 Cloudflare Dashboard 设置 SECRET_TOKEN",
  },
  "en-US": {
    guide_title: "📁 Mfile — User Guide",
    upload_section: "📤 Upload File",
    upload_body: "Send a file to @{up}, and @{idx} will reply with a file code.",
    upload_warn: "⚠️ Please start @{idx} first (send /start) to ensure you can receive the file code.",
    decode_section: "🔍 Decode",
    decode_body: "Send the file code to @{idx}, and @{dsp} will send the file to you.",
    decode_warn: "⚠️ Please start @{dsp} first (send /start) to ensure you can receive the file.",
    receive_section: "📥 Receive File",
    receive_body: "After successful decoding, @{dsp} will automatically send the file to you.",
    disclaimer_section: "⚠️ Disclaimer",
    disclaimer_body: "Users are responsible for uploaded content. This service only provides guidance and is not responsible for file content.",
    channel_section: "📢 Official Channel: {link}",
    quick_start: "🔗 Quick Start",
    btn_upload: "📤 Upload File",
    btn_decode: "🔍 Decode",
    btn_receive: "📥 Receive File",
    fallback_hint: "Please send /start for instructions",
    fatal_no_secret: "[FileBot][FATAL] SECRET_TOKEN not configured. Rejecting all requests to prevent webhook forgery. Please set SECRET_TOKEN in Cloudflare Dashboard",
  },
};

// 默认 locale(CF Worker 无用户偏好时使用)
const DEFAULT_LOCALE = "zh-CN";

/**
 * 根据消息推断用户 locale。
 *
 * 启发式策略:
 *  1. msg.from.language_code 优先(如 "zh"、"en")
 *  2. 前缀匹配:zh* → zh-CN,en* → en-US
 *  3. fallback 到 DEFAULT_LOCALE
 *
 * @param {object} msg - Telegram message 对象
 * @returns {string} locale 标识符("zh-CN" 或 "en-US")
 */
function detectLocale(msg) {
  const code = String(msg?.from?.language_code || "").toLowerCase();
  if (code.startsWith("zh")) return "zh-CN";
  if (code.startsWith("en")) return "en-US";
  return DEFAULT_LOCALE;
}

/**
 * 从 locale 消息字典取文案,支持 {var} 插值。
 *
 * @param {string} locale - locale 标识符
 * @param {string} key - 文案 key
 * @param {object} [vars] - 插值变量(如 {up: "up_bot"})
 * @returns {string} 渲染后的文案;key 不存在时回退到 zh-CN 再回退到 key 本身
 */
function t(locale, key, vars) {
  const candidates = [locale, DEFAULT_LOCALE];
  let text = undefined;
  for (const loc of candidates) {
    const dict = LOCALE_MESSAGES[loc];
    if (dict && Object.prototype.hasOwnProperty.call(dict, key)) {
      text = dict[key];
      break;
    }
  }
  if (text === undefined) return key;
  if (vars && typeof text === "string") {
    for (const [k, v] of Object.entries(vars)) {
      text = text.split(`{${k}}`).join(String(v));
    }
  }
  return text;
}

// ─── 请求 body 大小上限(1MB) ────────────────────────────────────
// Telegram webhook payload 通常 < 100KB,1MB 足以容纳最大消息+附件元数据,
// 同时防止恶意构造超大 JSON 触发 OOM。
const MAX_BODY_BYTES = 1 * 1024 * 1024;

// ─── update_id 持久去重 ─────────────────────────────────────────
// R51 P1-8: 多 isolate 下内存 debounce 不可靠(不同 isolate 不共享内存),
// 同一用户 60s 内可能收到两次回复。改用 KV 持久化 update_id 去重。
//
// 实现策略:
//  - 若 env.UPDATE_ID_KV 已绑定(Cloudflare KV namespace),使用 KV 持久去重:
//    get(update_id) → 若存在则跳过(已处理);否则 put(update_id, 1, TTL=86400)
//  - 若未绑定 KV,跳过去重(添加注释说明),仅依赖 Telegram 的 update_id 单调递增特性
//    做幂等(同一 update_id 多次到达时,handler 应能安全重复执行)。
//
// 注:CF Worker KV 默认 60 秒传播延迟,极端情况下仍可能重复(可接受)。
//     如需强一致去重,应改用 Durable Object(本引导 bot 不需要)。
const UPDATE_ID_KV_TTL_SECONDS = 86400; // 24 小时

/**
 * 检查 update_id 是否已处理过(基于 KV)。
 *
 * @param {number} updateId - Telegram update_id
 * @param {object} env - Worker env(含 UPDATE_ID_KV 绑定)
 * @returns {Promise<boolean>} true=已处理过(应跳过);false=未处理(继续)
 */
async function isUpdateIdProcessed(updateId, env) {
  if (!env || !env.UPDATE_ID_KV) {
    // 未绑定 KV:跳过去重(handler 应能安全幂等执行)
    return false;
  }
  try {
    const key = `tg_update:${updateId}`;
    const existing = await env.UPDATE_ID_KV.get(key);
    return existing !== null;
  } catch (e) {
    // KV 读失败:不阻塞流程,按"未处理"继续(可能重复,可接受)
    console.warn(`[FileBot] KV get update_id failed: ${e?.message || "unknown"}`);
    return false;
  }
}

/**
 * 标记 update_id 为已处理(写入 KV)。
 *
 * @param {number} updateId - Telegram update_id
 * @param {object} env - Worker env
 * @returns {Promise<void>}
 */
async function markUpdateIdProcessed(updateId, env) {
  if (!env || !env.UPDATE_ID_KV) return;
  try {
    const key = `tg_update:${updateId}`;
    await env.UPDATE_ID_KV.put(key, "1", { expirationTtl: UPDATE_ID_KV_TTL_SECONDS });
  } catch (e) {
    // KV 写失败:仅日志,不阻塞(下次可能重复处理,可接受)
    console.warn(`[FileBot] KV put update_id failed: ${e?.message || "unknown"}`);
  }
}

// ─── 引导文本 ───────────────────────────────────────────────────
/**
 * 构建引导文本(根据 locale 渲染)。
 *
 * @param {string} locale - locale 标识符
 * @param {string} up - 上传 bot 用户名
 * @param {string} idx - 索引 bot 用户名
 * @param {string} dsp - 派发 bot 用户名
 * @param {string} channelLink - 官方频道链接(可空)
 * @returns {string} 引导文本
 */
function buildGuide(locale, up, idx, dsp, channelLink) {
  const vars = { up, idx, dsp };
  let guide =
    `${t(locale, "guide_title")}\n\n` +
    `${t(locale, "upload_section")}\n` +
    `${t(locale, "upload_body", vars)}\n` +
    `${t(locale, "upload_warn", vars)}\n\n` +
    `${t(locale, "decode_section")}\n` +
    `${t(locale, "decode_body", vars)}\n` +
    `${t(locale, "decode_warn", vars)}\n\n` +
    `${t(locale, "receive_section")}\n` +
    `${t(locale, "receive_body", vars)}\n\n` +
    `${t(locale, "disclaimer_section")}\n` +
    `${t(locale, "disclaimer_body")}`;
  if (channelLink) {
    guide += `\n\n${t(locale, "channel_section", { link: channelLink })}`;
  }
  guide += `\n\n${t(locale, "quick_start")}`;
  return guide;
}

/**
 * 构建引导按钮(根据 locale 渲染按钮文案)。
 *
 * @param {string} locale - locale 标识符
 * @param {string} up - 上传 bot 用户名
 * @param {string} idx - 索引 bot 用户名
 * @param {string} dsp - 派发 bot 用户名
 * @returns {object|undefined} inline_keyboard 对象;无按钮时 undefined
 */
function buildKeyboard(locale, up, idx, dsp) {
  const buttons = [];
  if (up) buttons.push({ text: t(locale, "btn_upload"), url: `https://t.me/${up}` });
  if (idx) buttons.push({ text: t(locale, "btn_decode"), url: `https://t.me/${idx}` });
  if (dsp) buttons.push({ text: t(locale, "btn_receive"), url: `https://t.me/${dsp}` });
  return buttons.length ? { inline_keyboard: [buttons] } : undefined;
}

// ─── Telegram API ───────────────────────────────────────────────
/**
 * 调用 Telegram sendMessage API。
 *
 * R51 P1-8 安全整改:
 *  - 失败时只记录 HTTP status code,不记录 body(body 可能含敏感信息如 chat_id、user_id)
 *  - 异常时 console.warn 仅记录 error.message,不记录 stack(避免泄露内部路径)
 *
 * @param {number} chatId - 目标 chat ID
 * @param {string} text - 消息文本
 * @param {object|undefined} replyMarkup - inline_keyboard
 * @param {string} token - Bot token
 * @returns {Promise<void>}
 */
async function sendMessage(chatId, text, replyMarkup, token) {
  const body = {
    chat_id: chatId,
    text,
    disable_web_page_preview: true,
  };
  if (replyMarkup) body.reply_markup = replyMarkup;

  try {
    const resp = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      // R51 P1-8: 只记录 status code,不读取 body(body 可能含敏感信息)
      console.warn(`[FileBot] sendMessage failed: HTTP ${resp.status}`);
    }
  } catch (e) {
    // R51 P1-8: 仅记录 error.message,不含 stack/敏感信息
    console.warn(`[FileBot] sendMessage network error: ${e?.message || "unknown"}`);
  }
}

// ─── 消息处理 ───────────────────────────────────────────────────
/**
 * 处理 Telegram 消息(私聊隔离 + 文案 locale 化)。
 *
 * R51 P1-8: 移除内存 debounce(多 isolate 不可靠),改用 update_id 持久去重
 *           (在 fetch 入口处基于 KV 判断)。
 *
 * @param {object} msg - Telegram message 对象
 * @param {string} token - Bot token
 * @param {string} up - 上传 bot 用户名
 * @param {string} idx - 索引 bot 用户名
 * @param {string} dsp - 派发 bot 用户名
 * @param {string} channelLink - 官方频道链接(可空)
 * @returns {Promise<void>}
 */
async function handleMessage(msg, token, up, idx, dsp, channelLink) {
  const chat = msg?.chat;
  if (!chat || chat.type !== "private") return;
  const uid = msg?.from?.id;
  if (!uid) return;

  const locale = detectLocale(msg);
  const text = msg?.text || "";
  try {
    if (text === "/start") {
      await sendMessage(
        chat.id,
        buildGuide(locale, up, idx, dsp, channelLink),
        buildKeyboard(locale, up, idx, dsp),
        token,
      );
    } else {
      await sendMessage(chat.id, t(locale, "fallback_hint"), undefined, token);
    }
  } catch (e) {
    // R51 P1-8: catch 块添加日志(仅 error.message,不含敏感信息)
    console.warn(`[FileBot] handleMessage error: ${e?.message || "unknown"}`);
  }
}

// ─── Workers 入口 ───────────────────────────────────────────────

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

/**
 * R51 P1-8: 校验 Telegram update 基本 schema。
 *
 * 仅校验顶层字段存在性,不深递归(避免对恶意 payload 浪费 CPU)。
 *
 * @param {any} body - 解析后的 JSON body
 * @returns {boolean} true=通过;false=不通过
 */
function isValidTelegramUpdate(body) {
  if (!body || typeof body !== "object") return false;
  // 至少含 message / edited_message / channel_post / callback_query 之一
  if (
    !body.message &&
    !body.edited_message &&
    !body.channel_post &&
    !body.callback_query
  ) {
    return false;
  }
  // update_id 应为数字(Telegram 保证)
  if (body.update_id !== undefined && typeof body.update_id !== "number") {
    return false;
  }
  return true;
}

export default {
  async fetch(req, env) {
    // 强制验证 webhook secret token,防止伪造更新
    // 需在 Cloudflare Dashboard → Workers → Settings → Variables 中设置 SECRET_TOKEN
    // 或通过 wrangler secret put SECRET_TOKEN 设置
    // 安全默认:未配置 SECRET_TOKEN 时拒绝所有请求,防止任意用户伪造 Telegram update
    const secret = env.SECRET_TOKEN;
    if (!secret) {
      console.error(t(DEFAULT_LOCALE, "fatal_no_secret"));
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

    // R51 P1-8: Content-Type 必须为 JSON
    const contentType = req.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      return new Response("Unsupported Media Type", { status: 415 });
    }

    // R51 P1-8: 请求 body 大小上限 1MB(防止恶意构造超大 JSON OOM)
    const contentLength = parseInt(req.headers.get("content-length") || "0", 10);
    if (contentLength > MAX_BODY_BYTES) {
      console.warn(`[FileBot] body too large: ${contentLength} > ${MAX_BODY_BYTES}`);
      return new Response("Payload Too Large", { status: 413 });
    }

    // R51 P1-8: 解析 JSON + 基本 schema 校验
    let body;
    try {
      // 二次防御:即使 Content-Length 伪造,也限制实际读取字节数
      const rawText = await req.text();
      if (rawText.length > MAX_BODY_BYTES) {
        console.warn(`[FileBot] actual body too large: ${rawText.length} > ${MAX_BODY_BYTES}`);
        return new Response("Payload Too Large", { status: 413 });
      }
      body = JSON.parse(rawText);
    } catch (e) {
      // R51 P1-8: catch 块添加日志(仅 error.message,不含敏感信息)
      console.warn(`[FileBot] JSON parse failed: ${e?.message || "unknown"}`);
      return new Response("Bad Request", { status: 400 });
    }

    // R51 P1-8: 基本 schema 校验
    if (!isValidTelegramUpdate(body)) {
      console.warn("[FileBot] invalid Telegram update schema");
      return new Response("Bad Request", { status: 400 });
    }

    // R51 P1-8: update_id 持久去重(若绑定 UPDATE_ID_KV)
    if (body.update_id !== undefined) {
      const processed = await isUpdateIdProcessed(body.update_id, env);
      if (processed) {
        // 已处理过,返回 OK 不再触发 handler
        return new Response("OK");
      }
      await markUpdateIdProcessed(body.update_id, env);
    }

    // 处理消息(仅处理 message 类型,其他类型忽略)
    if (body.message) {
      try {
        await handleMessage(
          body.message,
          env.BOT_TOKEN,
          env.UP_BOT,
          env.IDX_BOT,
          env.DSP_BOT,
          env.CHANNEL_LINK || "",
        );
      } catch (e) {
        // R51 P1-8: catch 块添加日志(仅 error.message,不含敏感信息)
        console.warn(`[FileBot] top-level handler error: ${e?.message || "unknown"}`);
      }
    }
    return new Response("OK");
  },
};
