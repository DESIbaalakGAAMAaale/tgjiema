"""拓扑生成器 — 5 账号轮转配对 + R100 独立兜底

配对策略(5 账号滑动窗口):
  对于第 g 组(g 从 1 开始):
    Active  = 账号 ((g-1) % 5)
    Shadow1 = 账号 (g % 5)
    Shadow2 = 账号 ((g+1) % 5)

确保每组 3 个插槽来自 3 个不同的 Telegram 账号,任何单一账号被封,
另外两个账号的频道仍可维持该组的冗余。

R100:不接入环形链表,仅作最终兜底存档。

轮转参数优先从 DB rotation_config 读取,其次从 .env,最后从 groups.yaml。

用法:
    python config/generate_topology.py
"""

import os
import sys

# 确保项目根目录在 sys.path 中（从项目根目录 python config/generate_topology.py 运行时需要）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml


def _load_rotation_from_db_or_env(mon_cfg: dict) -> dict:
    """从 DB rotation_config 表读取轮转参数,优先 .env 作为默认值,DB 有值则覆盖 .env。"""
    from config import settings as _settings

    result = {
        "active_window_size": mon_cfg.get("active_window_size", 3),
        "rotation_files_per_slot": mon_cfg.get("rotation_files_per_slot", 500),
        "rotation_time_per_slot": mon_cfg.get("rotation_time_per_slot", 3600),
        "heartbeat_interval": mon_cfg.get("heartbeat_interval", 30),
        "heartbeat_timeout": mon_cfg.get("heartbeat_timeout", 90),
        "degrade_cooldown": mon_cfg.get("degrade_cooldown", 300),
    }

    # .env 作为默认值(优先级高于 groups.yaml 默认值)
    if hasattr(_settings, "ROTATION_ACTIVE_WINDOW_SIZE"):
        result["active_window_size"] = _settings.ROTATION_ACTIVE_WINDOW_SIZE
    if hasattr(_settings, "ROTATION_FILES_PER_SLOT"):
        result["rotation_files_per_slot"] = _settings.ROTATION_FILES_PER_SLOT
    if hasattr(_settings, "ROTATION_TIME_PER_SLOT"):
        result["rotation_time_per_slot"] = _settings.ROTATION_TIME_PER_SLOT

    try:
        import asyncio
        from database import init_db, close_db, get_rotation_config

        asyncio.get_event_loop().run_until_complete(init_db())

        db_keys = {
            "active_window_size": "active_window_size",
            "rotation_files_per_slot": "rotation_files_per_slot",
            "rotation_time_per_slot": "rotation_time_per_slot",
        }
        for key, db_key in db_keys.items():
            val = asyncio.get_event_loop().run_until_complete(get_rotation_config(db_key))
            if val and val.isdigit():
                result[key] = int(val)

        asyncio.get_event_loop().run_until_complete(close_db())
    except Exception as e:
        print(f"[警告] 无法从 DB 读取轮转配置,使用默认值: {e}")

    return result


def generate(groups_path: str = None, output_path: str = None, env_config: dict = None):
    base = os.path.dirname(os.path.abspath(__file__))
    if groups_path is None:
        groups_path = os.path.join(base, "groups.yaml")
    if output_path is None:
        output_path = os.path.join(base, "topology.yaml")

    # ── 优先使用 .env 配置(env_config),无则回退到 groups.yaml ──
    if env_config is None:
        try:
            from config import settings as _s
            env_config = _s.get_accounts_config()
        except Exception:
            env_config = {"accounts": [], "r100": {"channel": None, "fallback": []}}

    accounts = env_config.get("accounts", [])
    r100_cfg = env_config.get("r100", {})
    mon_cfg = env_config.get("mon", {})

    # 如果 .env 中没有配置账号,回退到 groups.yaml
    if not accounts:
        if not os.path.exists(groups_path):
            print("[错误] .env 中未配置账号频道,且未找到 config/groups.yaml")
            sys.exit(1)

        with open(groups_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        accounts = cfg.get("accounts", [])
        r100_cfg = cfg.get("r100", {})
        mon_cfg = cfg.get("mon", {})
        print("[info] 从 groups.yaml 读取账号配置")
    else:
        print(f"[info] 从 .env 读取账号配置: {len(accounts)} 个账号")

    if not accounts:
        print("[错误] 未配置任何账号频道,请在 .env 或 groups.yaml 中配置")
        sys.exit(1)

    account_count = len(accounts)

    # ── 校验:每个账号频道数相同,且为 3 的倍数 ──
    ch_counts = [len(a.get("channels", [])) for a in accounts]
    if len(set(ch_counts)) != 1:
        for a in accounts:
            print(f"  {a.get('name', '?')}: {len(a.get('channels', []))} 个频道")
        print("[错误] 所有账号的频道数必须相同")
        sys.exit(1)

    ch_per_account = ch_counts[0]
    if ch_per_account % 3 != 0:
        print(f"[错误] 每个账号频道数必须是 3 的倍数,当前: {ch_per_account}")
        sys.exit(1)

    group_count = (account_count * ch_per_account) // 3

    print(f"[校验] {account_count} 个账号 × {ch_per_account} 频道 = {account_count * ch_per_account} 总频道 → {group_count} 组")

    # ── 频道 ID 重复检测 ──
    all_ids = []
    for a in accounts:
        all_ids.extend(a.get("channels", []))
    if r100_cfg.get("channel"):
        all_ids.append(r100_cfg["channel"])
    for fb in (r100_cfg.get("fallback") or []):
        all_ids.append(fb)

    if len(all_ids) != len(set(all_ids)):
        from collections import Counter
        dupes = [ch for ch, cnt in Counter(all_ids).items() if cnt > 1]
        print(f"[错误] 检测到重复频道 ID: {dupes}")
        for ch in dupes:
            owners = [a.get("name", "?") for a in accounts if ch in a.get("channels", [])]
            if r100_cfg.get("channel") == ch:
                owners.append("R100")
            if ch in (r100_cfg.get("fallback") or []):
                owners.append("R100-fallback")
            print(f"  {ch} 出现在: {owners}")
        sys.exit(1)

    # ── 为每个账号建立频道池 + 游标 ──
    pools = []
    for a in accounts:
        pools.append({
            "name": a.get("name", "?"),
            "channels": list(a.get("channels", [])),
            "cursor": 0,
        })

    # ── 轮转配对 ──
    groups = []

    for g in range(1, group_count + 1):
        ai = (g - 1) % account_count  # Active 账号索引
        s1i = g % account_count       # Shadow1 账号索引
        s2i = (g + 1) % account_count # Shadow2 账号索引

        a_ch = pools[ai]["channels"][pools[ai]["cursor"]]
        pools[ai]["cursor"] += 1

        s1_ch = pools[s1i]["channels"][pools[s1i]["cursor"]]
        pools[s1i]["cursor"] += 1

        s2_ch = pools[s2i]["channels"][pools[s2i]["cursor"]]
        pools[s2i]["cursor"] += 1

        groups.append({
            "id": g,
            "desc": f"A={pools[ai]['name']} / S1={pools[s1i]['name']} / S2={pools[s2i]['name']}",
            "active": a_ch,
            "shadow1": s1_ch,
            "shadow2": s2_ch,
        })

    # ── R100 独立槽位 ──
    r100_channel = r100_cfg.get("channel")
    if r100_channel is None:
        print("[警告] 未配置 R100 兜底频道,跳过")
    r100_fallback = r100_cfg.get("fallback", []) or []

    # ── Mon 配置 ──
    rotation = _load_rotation_from_db_or_env(mon_cfg)

    # ── 构建拓扑结构 ──
    slots = []
    active_channel_ids = []

    # 构建 channel_id → account_name 映射(用于 topology 输出)
    ch_to_account = {}
    for a in accounts:
        for ch in a.get("channels", []):
            ch_to_account[ch] = a.get("name", "?")

    for g in groups:
        gn = g["id"]
        aid = f"a{gn}"
        s1id = f"s{gn}a"
        s2id = f"s{gn}b"

        s2_entry = {
            "slot_id": s2id,
            "channel_id": g["shadow2"],
            "status": "shadow2",
            "next_active_chat_id": g["active"],
            "prev_slot_id": s1id,
            "account_name": ch_to_account.get(g["shadow2"], ""),
        }

        s1_entry = {
            "slot_id": s1id,
            "channel_id": g["shadow1"],
            "status": "shadow1",
            "next_active_chat_id": None,
            "prev_slot_id": aid,
            "account_name": ch_to_account.get(g["shadow1"], ""),
        }

        a_entry = {
            "slot_id": aid,
            "channel_id": g["active"],
            "status": "active",
            "next_active_chat_id": None,
            "prev_slot_id": s2id,
            "account_name": ch_to_account.get(g["active"], ""),
        }

        slots.extend([a_entry, s1_entry, s2_entry])
        active_channel_ids.append((aid, g["active"]))

    # ── 填充环形链表(仅常规组,不含 R100) ──
    for i, (aid, _) in enumerate(active_channel_ids):
        next_i = (i + 1) % len(active_channel_ids)
        next_ch = active_channel_ids[next_i][1]
        for s in slots:
            if s["slot_id"] == aid:
                s["next_active_chat_id"] = next_ch
                break

    # ── 输出 YAML ──
    lines = [
        "# 环形冗余拓扑配置(自动生成,请勿手动编辑)",
        f"# 配置来源: .env 或 config/groups.yaml",
        f"# 共 {len(accounts)} 个账号,{len(groups)} 组,{len(slots)} 个槽位",
        f"# 配对策略: 5 账号轮转滑动窗口,每组 A/S1/S2 三不同账号",
        "#",
        "slots:",
    ]

    groups_slots = [slots[i*3:(i+1)*3] for i in range(len(groups))]

    for gi, (g, grp_slots) in enumerate(zip(groups, groups_slots)):
        gn = g["id"]
        desc = g.get("desc", "")
        lines.append(f"  # ─── 组 {gn} — {desc} ───")

        for s in grp_slots:
            lines.append(f"  - slot_id: \"{s['slot_id']}\"")
            lines.append(f"    channel_id: {s['channel_id']}")
            lines.append(f"    status: \"{s['status']}\"")
            lines.append(f"    account_name: \"{s.get('account_name', '')}\"")
            nxt = s["next_active_chat_id"]
            lines.append(f"    next_active_chat_id: {nxt if nxt is not None else 'null'}")
            lines.append(f"    prev_slot_id: \"{s['prev_slot_id']}\"")
            lines.append("")

    # ── R100 独立槽位(不参与环形链表) ──
    if r100_channel:
        lines.append("  # ─── R100 最终兜底(不接入环形调度)───")
        lines.append("  - slot_id: \"r100\"")
        lines.append(f"    channel_id: {r100_channel}")
        lines.append("    status: \"r100\"")
        lines.append("    account_name: \"R100\"")
        lines.append("    next_active_chat_id: null")
        lines.append("    prev_slot_id: null")
        lines.append("    is_r100: true")
        lines.append("")
        # R100 备选
        all_r100 = [r100_channel] + r100_fallback
        for fi, fb_ch in enumerate(r100_fallback):
            lines.append(f"  - slot_id: \"r100-fb{fi+1}\"")
            lines.append(f"    channel_id: {fb_ch}")
            lines.append("    status: \"r100-fallback\"")
            lines.append("    account_name: \"R100\"")
            lines.append("    next_active_chat_id: null")
            lines.append("    prev_slot_id: null")
            lines.append("    is_r100: true")
            lines.append("")

    lines.append("# Mon 监控 + 轮转配置")
    lines.append("mon:")
    lines.append(f"  active_window_size: {rotation.get('active_window_size', 3)}")
    lines.append(f"  rotation_files_per_slot: {rotation.get('rotation_files_per_slot', 500)}")
    lines.append(f"  rotation_time_per_slot: {rotation.get('rotation_time_per_slot', 3600)}")
    lines.append(f"  heartbeat_interval: {rotation.get('heartbeat_interval', 30)}")
    lines.append(f"  heartbeat_timeout: {rotation.get('heartbeat_timeout', 90)}")
    lines.append(f"  degrade_cooldown: {rotation.get('degrade_cooldown', 300)}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # ── 汇总 + 验证 ──
    print(f"\n[生成] {len(accounts)} 账号 → {len(groups)} 组 → {len(slots)} 槽位")
    print(f"[策略] 5 账号轮转滑动窗口: 每组 A/S1/S2 三不同账号")
    if r100_channel:
        print(f"[R100] 兜底频道 {r100_channel}(不参与环形调度)")

    # 验证:每个账号的用量
    usage = {}
    for a in accounts:
        usage[a.get("name", "?")] = {"A": 0, "S1": 0, "S2": 0}
    for g in groups:
        desc = g["desc"]
        # Parse "A=账号1 / S1=账号2 / S2=账号3"
        parts = [p.strip() for p in desc.split("/")]
        for p in parts:
            role, name = [x.strip() for x in p.split("=", 1)]
            usage[name][role] += 1

    print(f"\n[账号用量验证]")
    for name, counts in usage.items():
        total = sum(counts.values())
        print(f"  {name}: A={counts['A']} S1={counts['S1']} S2={counts['S2']} = {total}")

    print(f"\n[输出] {output_path}")
    print(f"[下一步] python admin/seed_topology.py --yes")


if __name__ == "__main__":
    generate()