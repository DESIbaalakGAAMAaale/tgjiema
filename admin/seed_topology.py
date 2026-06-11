"""拓扑初始化脚本
将 config/groups.yaml 的槽位配置自动生成 topology.yaml 并写入数据库 cells 表。
同时初始化备用池和轮转配置表。
仅在全新部署或拓扑重建时使用。

用法：
    python admin/seed_topology.py           # 写入数据库
    python admin/seed_topology.py --dry-run # 仅生成 topology.yaml，不写库
    python admin/seed_topology.py --yes     # 跳过确认，直接写入
"""

import asyncio
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_db, close_db, get_cells_col, make_cell
from database import set_rotation_config, add_spare_channel


async def seed(dry_run: bool = False, force: bool = False):
    # ── 步骤1：自动生成 topology.yaml（如果 groups.yaml 更新） ──
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    groups_path = os.path.join(base, "config", "groups.yaml")
    topo_path = os.path.join(base, "config", "topology.yaml")

    if not os.path.exists(groups_path):
        print("[错误] 未找到 config/groups.yaml，请先创建拓扑配置")
        sys.exit(1)

    need_regenerate = True
    if os.path.exists(topo_path):
        groups_mtime = os.path.getmtime(groups_path)
        topo_mtime = os.path.getmtime(topo_path)
        need_regenerate = groups_mtime > topo_mtime

    if need_regenerate:
        print("[info] groups.yaml 有更新，重新生成 topology.yaml ...")
        from config.generate_topology import generate
        generate(groups_path, topo_path)
    else:
        print("[info] topology.yaml 已是最新，跳过生成")

    with open(topo_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    slots = config.get("slots", [])
    if not slots:
        print("[错误] topology.yaml 中没有槽位配置")
        sys.exit(1)

    print(f"\n拓扑概况: {len(slots)} 个槽位 ({len(slots)//3} 组)")
    active_count = len([s for s in slots if s["status"] in ("active", "r100")])
    shadow_count = len([s for s in slots if s["status"].startswith("shadow")])
    print(f"  Active: {active_count} | Shadow: {shadow_count}")

    if dry_run:
        print("\n[Dry Run] topology.yaml 已生成，跳过数据库写入")
        return

    # ── 步骤2：用户确认 ──
    if not force:
        print("\n即将写入数据库 cells 表...")
        resp = input("确认执行? (y/N): ").strip().lower()
        if resp != "y":
            print("已取消")
            return

    # ── 步骤3：写入数据库 ──
    await init_db()
    col = get_cells_col()

    existing = await col.find({})
    existing_slot_ids = {r["slot_id"] for r in existing}

    print(f"\n数据库现有 {len(existing)} 个槽位，配置 {len(slots)} 个")

    added = 0
    updated = 0
    for slot in slots:
        sid = slot["slot_id"]
        update_data = {
            "channel_id": slot["channel_id"],
            "status": slot["status"],
            "next_active_chat_id": slot.get("next_active_chat_id"),
            "prev_slot_id": slot.get("prev_slot_id"),
            "account_name": slot.get("account_name", ""),
            "is_r100": 1 if slot.get("is_r100") else 0,
        }

        if sid in existing_slot_ids:
            await col.update_one({"slot_id": sid}, {"$set": update_data})
            updated += 1
            print(f"  [update] {sid} → channel={slot['channel_id']} status={slot['status']}")
        else:
            cell = make_cell(
                slot_id=sid,
                channel_id=slot["channel_id"],
                status=slot["status"],
                next_active_chat_id=slot.get("next_active_chat_id"),
                prev_slot_id=slot.get("prev_slot_id"),
                is_r100=slot.get("is_r100", False),
                account_name=slot.get("account_name", ""),
            )
            await col.insert_one(cell)
            added += 1
            print(f"  [create] {sid} → channel={slot['channel_id']} status={slot['status']}")

    await close_db()
    print(f"\n完成: 新增 {added} 个, 更新 {updated} 个")

    # ── 步骤4：初始化轮转配置（如果不存在） ──
    await init_db()
    mon_cfg = config.get("mon", {})
    defaults = {
        "active_window_size": str(mon_cfg.get("active_window_size", 3)),
        "rotation_files_per_slot": str(mon_cfg.get("rotation_files_per_slot", 500)),
        "rotation_time_per_slot": str(mon_cfg.get("rotation_time_per_slot", 3600)),
    }
    for key, val in defaults.items():
        await set_rotation_config(key, val)
    print(f"轮转配置已初始化: {defaults}")
    await close_db()

    print("下一步: python run_all.py")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    yes = "--yes" in sys.argv
    asyncio.run(seed(dry_run=dry, force=yes))


async def auto_seed():
    """自动初始化拓扑（启动时静默调用，不交互）。"""
    print("[seed] 自动初始化拓扑...")
    await seed(dry_run=False, force=True)
    print("[seed] 拓扑初始化完成")