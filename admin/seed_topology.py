"""拓扑初始化脚本
将 config/topology.yaml 的槽位配置写入数据库 cells 表。
仅在全新部署或拓扑重建时使用。
"""

import asyncio
import yaml
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_db, close_db, get_cells_col, make_cell


async def seed():
    topo_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "topology.yaml",
    )
    with open(topo_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    slots = config.get("slots", [])
    col = get_cells_col()

    await init_db()

    existing = await col.find({})
    existing_slot_ids = {r["slot_id"] for r in existing}

    print(f"[seed] 拓扑配置 {len(slots)} 个槽位, 数据库现有 {len(existing)} 个")

    added = 0
    for slot in slots:
        sid = slot["slot_id"]
        if sid in existing_slot_ids:
            # 已有记录，更新关键字段
            await col.update_one(
                {"slot_id": sid},
                {"$set": {
                    "channel_id": slot["channel_id"],
                    "status": slot["status"],
                    "next_active_chat_id": slot.get("next_active_chat_id"),
                    "prev_slot_id": slot.get("prev_slot_id"),
                    "is_r100": 1 if slot.get("is_r100") else 0,
                }},
            )
            print(f"  [update] {sid} → channel={slot['channel_id']} status={slot['status']}")
        else:
            cell = make_cell(
                slot_id=sid,
                channel_id=slot["channel_id"],
                status=slot["status"],
                next_active_chat_id=slot.get("next_active_chat_id"),
                prev_slot_id=slot.get("prev_slot_id"),
                is_r100=slot.get("is_r100", False),
            )
            await col.insert_one(cell)
            added += 1
            print(f"  [create] {sid} → channel={slot['channel_id']} status={slot['status']}")

    print(f"[seed] 完成: 新增 {added}, 更新 {len(slots) - added}")
    await close_db()


if __name__ == "__main__":
    asyncio.run(seed())