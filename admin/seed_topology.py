"""拓扑初始化脚本
将 config/groups.yaml 的槽位配置自动生成 topology.yaml 并写入数据库 cells 表。
同时初始化备用池和轮转配置表。
仅在全新部署或拓扑重建时使用。

用法:
    python admin/seed_topology.py           # 写入数据库
    python admin/seed_topology.py --dry-run # 仅生成 topology.yaml,不写库
    python admin/seed_topology.py --yes     # 跳过确认,直接写入
"""

import asyncio
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_db, close_db, get_cells_col
from database import set_rotation_config


async def seed(dry_run: bool = False, force: bool = False):
    # ── 步骤1:始终从 .env 实时生成拓扑(不依赖 git 缓存文件中的占位 ID) ──
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    groups_path = os.path.join(base, "config", "groups.yaml")
    topo_path = os.path.join(base, "config", "topology.yaml")

    print("[info] 从 .env 账号配置实时生成拓扑...")
    from config.generate_topology import generate
    generate(groups_path, topo_path, skip_db_lookup=True)

    with open(topo_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    slots = config.get("slots", [])
    if not slots:
        print("[错误] topology.yaml 中没有槽位配置")
        raise RuntimeError("topology.yaml 中没有槽位配置")

    print(f"\n拓扑概况: {len(slots)} 个槽位 ({len(slots)//3} 组)")
    active_count = len([s for s in slots if s["status"] in ("active", "r100")])
    shadow_count = len([s for s in slots if s["status"].startswith("shadow")])
    print(f"  Active: {active_count} | Shadow: {shadow_count}")

    if dry_run:
        print("\n[Dry Run] topology.yaml 已生成,跳过数据库写入")
        return

    # ── 步骤2:用户确认 ──
    if not force:
        print("\n即将写入数据库 cells 表...")
        resp = input("确认执行? (y/N): ").strip().lower()
        if resp != "y":
            print("已取消")
            return

    # ── 步骤3:写入数据库 ──
    await init_db()
    col = get_cells_col()

    existing = await col.find({}, projection=["slot_id", "channel_id", "status",
                                                "next_active_chat_id", "prev_slot_id",
                                                "account_name", "is_r100"])
    existing_map = {r["slot_id"]: r for r in existing}

    # force=True 时清空旧数据重新写入，确保拓扑与 .env / topology.yaml 一致
    if force and existing_map:
        await col.delete_many({})
        existing_map = {}
        print("  强制模式: 清空了数据库 cells 表，重新写入最新拓扑")

    print(f"\n数据库现有 {len(existing_map)} 个槽位,配置 {len(slots)} 个")

    added = 0
    skipped = 0
    for slot in slots:
        sid = slot["slot_id"]

        if sid in existing_map:
            skipped += 1
        else:
            from database import make_cell
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

    print(f"\n完成: 新增 {added} 个, 跳过 {skipped} 个(已存在)")

    # ── 步骤4:初始化轮转配置(仅当不存在时写入，不覆盖已有值) ──
    from database import get_rotation_config
    mon_cfg = config.get("mon", {})
    defaults = {
        "rotation_active_window_size": str(mon_cfg.get("active_window_size", 3)),
        "rotation_files_per_slot": str(mon_cfg.get("rotation_files_per_slot", 500)),
        "rotation_time_per_slot": str(mon_cfg.get("rotation_time_per_slot", 3600)),
    }
    initialized = 0
    for key, val in defaults.items():
        existing_val = await get_rotation_config(key)
        if existing_val is None:
            await set_rotation_config(key, val)
            initialized += 1
    print(f"轮转配置已初始化: {initialized}/{len(defaults)} 项(新增)")
    await close_db()

    print("下一步: python run_all.py")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    yes = "--yes" in sys.argv
    asyncio.run(seed(dry_run=dry, force=yes))


async def auto_seed():
    """自动初始化拓扑(启动时静默调用,不交互)。
    仅在 cells 表为空时写入，避免覆盖运行时状态。"""
    print("[seed] 自动初始化拓扑...")
    from database import init_db, close_db, get_cells_col
    await init_db()
    col = get_cells_col()
    cell_count = await col.count_documents({})
    await close_db()
    if cell_count > 0:
        print(f"[seed] cells 表已有 {cell_count} 个槽位,跳过初始化(避免覆盖运行时状态)")
        return
    await seed(dry_run=False, force=True)
    print("[seed] 拓扑初始化完成")