"""拓扑生成器 — 从 config/groups.yaml 自动生成 config/topology.yaml

新设计：
- 用户只需提供账号 + 频道列表
- 每 3 个频道自动编为一组 (Active, Shadow1, Shadow2)
- 环形链表指针完全自动计算

用法：
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

    # ── 扁平化：按账号顺序，每 3 个频道为一组 ──
    groups = []  # [(group_id, active_ch, shadow1_ch, shadow2_ch, is_r100)]
    group_idx = 0

    for account in accounts:
        channels = account.get("channels", [])
        account_name = account.get("name", "未知账号")

        if len(channels) % 3 != 0:
            print(f"[警告] {account_name}: 频道数 ({len(channels)}) 不是 3 的倍数，")
            print(f"        末尾 {len(channels) % 3} 个频道将被忽略")

        for i in range(0, len(channels) - 2, 3):
            group_idx += 1
            groups.append({
                "id": group_idx,
                "account": account_name,
                "active": channels[i],
                "shadow1": channels[i + 1],
                "shadow2": channels[i + 2],
                "r100": False,
            })

    if not groups:
        print("[错误] 未生成任何拓扑组，请检查频道配置")
        sys.exit(1)

    # ── R100: 最后一组或指定组 ──
    mon_cfg = cfg.get("mon", {})
    r100_group = mon_cfg.get("r100_group", -1)
    if r100_group == -1:
        r100_group = len(groups)
    if 1 <= r100_group <= len(groups):
        groups[r100_group - 1]["r100"] = True

    # ── 生成槽位 ──
    slots = []
    active_channel_ids = []  # [(slot_id, channel_id)]

    for g in groups:
        gn = g["id"]
        is_r100 = g["r100"]

        aid = f"a{gn}"
        s1id = f"s{gn}a"
        s2id = f"s{gn}b"
        a_status = "r100" if is_r100 else "active"

        # shadow2 → 同组 active
        s2_entry = {
            "slot_id": s2id,
            "channel_id": g["shadow2"],
            "status": "shadow2",
            "next_active_chat_id": g["active"],
            "prev_slot_id": s1id,
        }
        if is_r100:
            s2_entry["is_r100"] = True

        # shadow1
        s1_entry = {
            "slot_id": s1id,
            "channel_id": g["shadow1"],
            "status": "shadow1",
            "next_active_chat_id": None,
            "prev_slot_id": aid,
        }

        # active（next 待填充—指向下一组 active）
        a_entry = {
            "slot_id": aid,
            "channel_id": g["active"],
            "status": a_status,
            "next_active_chat_id": None,
            "prev_slot_id": s2id,
        }
        if is_r100:
            a_entry["is_r100"] = True

        slots.extend([a_entry, s1_entry, s2_entry])
        active_channel_ids.append((aid, g["active"]))

    # ── 填充环形链表 ──
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
        "#",
        "slots:",
    ]

    groups_slots = [slots[i*3:(i+1)*3] for i in range(len(groups))]

    for gi, (g, grp_slots) in enumerate(zip(groups, groups_slots)):
        gn = g["id"]
        acct = g["account"]
        is_r100 = g["r100"]
        label = f"组 {gn} — {acct}"
        if is_r100:
            label += " (R100)"
        lines.append(f"  # ─── {label} ───")

        for s in grp_slots:
            lines.append(f"  - slot_id: \"{s['slot_id']}\"")
            lines.append(f"    channel_id: {s['channel_id']}")
            lines.append(f"    status: \"{s['status']}\"")
            nxt = s["next_active_chat_id"]
            lines.append(f"    next_active_chat_id: {nxt if nxt is not None else 'null'}")
            lines.append(f"    prev_slot_id: \"{s['prev_slot_id']}\"")
            if s.get("is_r100"):
                lines.append(f"    is_r100: true")
            lines.append("")

    lines.append("# Mon 监控配置")
    lines.append("mon:")
    lines.append(f"  heartbeat_interval: {mon_cfg.get('heartbeat_interval', 30)}")
    lines.append(f"  heartbeat_timeout: {mon_cfg.get('heartbeat_timeout', 90)}")
    lines.append(f"  degrade_cooldown: {mon_cfg.get('degrade_cooldown', 300)}")
    lines.append(f"  r100_managed: {str(mon_cfg.get('r100_managed', False)).lower()}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[生成] {len(accounts)} 个账号 → {len(groups)} 组 → {len(slots)} 个槽位")
    print(f"[输出] {output_path}")
    print(f"[下一步] python admin/seed_topology.py --yes")


if __name__ == "__main__":
    generate()