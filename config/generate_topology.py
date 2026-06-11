"""拓扑生成器 — 5 账号轮转配对 + R100 独立兜底

配对策略（5 账号滑动窗口）：
  对于第 g 组（g 从 1 开始）:
    Active  = 账号 ((g-1) % 5)
    Shadow1 = 账号 (g % 5)
    Shadow2 = 账号 ((g+1) % 5)

确保每组 3 个插槽来自 3 个不同的 Telegram 账号，任何单一账号被封，
另外两个账号的频道仍可维持该组的冗余。

R100：不接入环形链表，仅作最终兜底存档。

用法:
    python config/generate_topology.py
"""

import os
import sys

import yaml


def generate(groups_path: str = None, output_path: str = None):
    base = os.path.dirname(os.path.abspath(__file__))
    if groups_path is None:
        groups_path = os.path.join(base, "groups.yaml")
    if output_path is None:
        output_path = os.path.join(base, "topology.yaml")

    with open(groups_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    accounts = cfg.get("accounts", [])
    if not accounts:
        print("[错误] groups.yaml 中没有配置任何账号")
        sys.exit(1)

    account_count = len(accounts)

    # ── 校验：每个账号频道数相同，且为 3 的倍数 ──
    ch_counts = [len(a.get("channels", [])) for a in accounts]
    if len(set(ch_counts)) != 1:
        for a in accounts:
            print(f"  {a.get('name', '?')}: {len(a.get('channels', []))} 个频道")
        print("[错误] 所有账号的频道数必须相同")
        sys.exit(1)

    ch_per_account = ch_counts[0]
    if ch_per_account % 3 != 0:
        print(f"[错误] 每个账号频道数必须是 3 的倍数，当前: {ch_per_account}")
        sys.exit(1)

    group_count = (account_count * ch_per_account) // 3

    print(f"[校验] {account_count} 个账号 × {ch_per_account} 频道 = {account_count * ch_per_account} 总频道 → {group_count} 组")

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
    r100_cfg = cfg.get("r100", {})
    r100_channel = r100_cfg.get("channel")
    if r100_channel is None:
        print("[警告] 未配置 R100 兜底频道，跳过")
    r100_fallback = r100_cfg.get("fallback", []) or []

    # ── Mon 配置 ──
    mon_cfg = cfg.get("mon", {})

    # ── 写入 topology.yaml ──
    _write_topology(accounts, groups, mon_cfg, r100_channel, r100_fallback, output_path)


def _write_topology(accounts, groups, mon_cfg, r100_channel, r100_fallback, output_path):
    slots = []
    active_channel_ids = []

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
        }

        s1_entry = {
            "slot_id": s1id,
            "channel_id": g["shadow1"],
            "status": "shadow1",
            "next_active_chat_id": None,
            "prev_slot_id": aid,
        }

        a_entry = {
            "slot_id": aid,
            "channel_id": g["active"],
            "status": "active",
            "next_active_chat_id": None,
            "prev_slot_id": s2id,
        }

        slots.extend([a_entry, s1_entry, s2_entry])
        active_channel_ids.append((aid, g["active"]))

    # ── 填充环形链表（仅常规组，不含 R100） ──
    for i, (aid, _) in enumerate(active_channel_ids):
        next_i = (i + 1) % len(active_channel_ids)
        next_ch = active_channel_ids[next_i][1]
        for s in slots:
            if s["slot_id"] == aid:
                s["next_active_chat_id"] = next_ch
                break

    # ── 输出 YAML ──
    lines = [
        "# 环形冗余拓扑配置（自动生成，请勿手动编辑）",
        f"# 源文件: config/groups.yaml",
        f"# 共 {len(accounts)} 个账号，{len(groups)} 组，{len(slots)} 个槽位",
        f"# 配对策略: 5 账号轮转滑动窗口，每组 A/S1/S2 三不同账号",
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
            nxt = s["next_active_chat_id"]
            lines.append(f"    next_active_chat_id: {nxt if nxt is not None else 'null'}")
            lines.append(f"    prev_slot_id: \"{s['prev_slot_id']}\"")
            lines.append("")

    # ── R100 独立槽位（不参与环形链表） ──
    if r100_channel:
        lines.append("  # ─── R100 最终兜底（不接入环形调度）───")
        lines.append("  - slot_id: \"r100\"")
        lines.append(f"    channel_id: {r100_channel}")
        lines.append("    status: \"r100\"")
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
            lines.append("    next_active_chat_id: null")
            lines.append("    prev_slot_id: null")
            lines.append("    is_r100: true")
            lines.append("")

    lines.append("# Mon 监控配置")
    lines.append("mon:")
    lines.append(f"  active_count: {mon_cfg.get('active_count', 3)}")
    lines.append(f"  heartbeat_interval: {mon_cfg.get('heartbeat_interval', 30)}")
    lines.append(f"  heartbeat_timeout: {mon_cfg.get('heartbeat_timeout', 90)}")
    lines.append(f"  degrade_cooldown: {mon_cfg.get('degrade_cooldown', 300)}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # ── 汇总 + 验证 ──
    print(f"\n[生成] {len(accounts)} 账号 → {len(groups)} 组 → {len(slots)} 槽位")
    print(f"[策略] 5 账号轮转滑动窗口: 每组 A/S1/S2 三不同账号")
    if r100_channel:
        print(f"[R100] 兜底频道 {r100_channel}（不参与环形调度）")

    # 验证：每个账号的用量
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