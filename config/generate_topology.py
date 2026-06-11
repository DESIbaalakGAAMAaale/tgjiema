"""拓扑生成器 — 跨账号交替换位分组

配对策略（2 个账号）:
  奇数组: Active(账号1) + Shadow1(账号2) + Shadow2(账号2)
  偶数组: Active(账号2) + Shadow1(账号1) + Shadow2(账号1)

确保每组 Active 和 Shadow 分布在不同的 Telegram 账号上，
任何一个账号被封，另一账号的 Shadow 可以无缝顶上。

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

    # ── 收集各账号频道 ──
    account_pools = []
    for acct in accounts:
        chs = acct.get("channels", [])
        account_pools.append({
            "name": acct.get("name", "未知"),
            "channels": list(chs),
            "cursor": 0,
        })

    total_channels = sum(len(p["channels"]) for p in account_pools)

    if len(account_pools) == 1:
        _generate_single_account(account_pools, cfg, output_path)
        return

    if len(account_pools) != 2:
        print(f"[错误] 当前仅支持 2 个账号的跨账号配对，检测到 {len(account_pools)} 个")
        sys.exit(1)

    master = account_pools[0]
    slave = account_pools[1]

    if len(master["channels"]) != len(slave["channels"]):
        print(f"[错误] 两个账号频道数必须相同: {master['name']}={len(master['channels'])}, {slave['name']}={len(slave['channels'])}")
        sys.exit(1)

    ch_count = len(master["channels"])
    if ch_count % 3 != 0:
        print(f"[错误] 每个账号频道数必须是 3 的倍数，当前: {ch_count}")
        sys.exit(1)

    group_count = total_channels // 3
    mi = 0  # master channel index
    si = 0  # slave channel index

    groups = []  # [{id, account, active, shadow1, shadow2, r100}]

    for g in range(1, group_count + 1):
        if g % 2 == 1:
            # 奇数组: Active 来自 master, Shadow 来自 slave
            a = master["channels"][mi]
            s1 = slave["channels"][si]
            s2 = slave["channels"][si + 1]
            mi += 1
            si += 2
            groups.append({
                "id": g,
                "account": f"{master['name']}(A) / {slave['name']}(S)",
                "active": a,
                "shadow1": s1,
                "shadow2": s2,
                "r100": False,
            })
        else:
            # 偶数组: Active 来自 slave, Shadow 来自 master
            a = slave["channels"][si]
            s1 = master["channels"][mi]
            s2 = master["channels"][mi + 1]
            si += 1
            mi += 2
            groups.append({
                "id": g,
                "account": f"{slave['name']}(A) / {master['name']}(S)",
                "active": a,
                "shadow1": s1,
                "shadow2": s2,
                "r100": False,
            })

    # ── R100: 最后一组 ──
    mon_cfg = cfg.get("mon", {})
    r100_group = mon_cfg.get("r100_group", -1)
    if r100_group == -1:
        r100_group = group_count
    if 1 <= r100_group <= group_count:
        groups[r100_group - 1]["r100"] = True

    # ── 生成槽位 ──
    _write_topology(accounts, groups, mon_cfg, output_path)


def _generate_single_account(account_pools, cfg, output_path):
    """单账号模式：简单顺序分组，无跨账号冗余。"""
    pool = account_pools[0]
    chs = pool["channels"]
    if len(chs) % 3 != 0:
        print(f"[错误] 频道数必须是 3 的倍数，当前: {len(chs)}")
        sys.exit(1)

    group_count = len(chs) // 3
    groups = []
    for g in range(group_count):
        off = g * 3
        groups.append({
            "id": g + 1,
            "account": pool["name"],
            "active": chs[off],
            "shadow1": chs[off + 1],
            "shadow2": chs[off + 2],
            "r100": False,
        })

    mon_cfg = cfg.get("mon", {})
    r100_group = mon_cfg.get("r100_group", -1)
    if r100_group == -1:
        r100_group = group_count
    if 1 <= r100_group <= group_count:
        groups[r100_group - 1]["r100"] = True

    _write_topology([pool], groups, mon_cfg, output_path)


def _write_topology(accounts, groups, mon_cfg, output_path):
    """将分组信息写入 topology.yaml。"""
    slots = []
    active_channel_ids = []

    for g in groups:
        gn = g["id"]
        is_r100 = g["r100"]

        aid = f"a{gn}"
        s1id = f"s{gn}a"
        s2id = f"s{gn}b"
        a_status = "r100" if is_r100 else "active"

        s2_entry = {
            "slot_id": s2id,
            "channel_id": g["shadow2"],
            "status": "shadow2",
            "next_active_chat_id": g["active"],
            "prev_slot_id": s1id,
        }
        if is_r100:
            s2_entry["is_r100"] = True

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
        f"# 配对策略: 奇数组 Active(账号1)/Shadow(账号2), 偶数组反之",
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
    lines.append(f"  active_count: {mon_cfg.get('active_count', 3)}")
    lines.append(f"  heartbeat_interval: {mon_cfg.get('heartbeat_interval', 30)}")
    lines.append(f"  heartbeat_timeout: {mon_cfg.get('heartbeat_timeout', 90)}")
    lines.append(f"  degrade_cooldown: {mon_cfg.get('degrade_cooldown', 300)}")
    lines.append(f"  r100_managed: {str(mon_cfg.get('r100_managed', False)).lower()}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # ── 汇总 ──
    print(f"[生成] {len(accounts)} 个账号 → {len(groups)} 组 → {len(slots)} 个槽位")
    print(f"[策略] 跨账号交替换位: 每组 Active/Shadow 在不同账号上")
    print(f"[输出] {output_path}")
    print(f"[下一步] python admin/seed_topology.py --yes")


if __name__ == "__main__":
    generate()