from fastapi import FastAPI, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
from pathlib import Path
import datetime

from database import get_users_col, get_file_records_col, get_decode_logs_col
from utils.monitor import metrics
from config import settings

app = FastAPI(title="TG解码器管理后台")
security = HTTPBasic()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"),
        settings.ADMIN_USERNAME.encode("utf8"),
    )
    correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"),
        settings.ADMIN_PASSWORD.encode("utf8"),
    )
    if not (correct_username and correct_password):
        raise HTTPException(status_code=401, detail="未授权访问")
    return credentials.username


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, admin=Depends(verify_admin)):
    users_col = get_users_col()
    files_col = get_file_records_col()
    logs_col = get_decode_logs_col()

    total_users = await users_col.count_documents({})
    total_files = await files_col.count_documents({})
    active_files = await files_col.count_documents({"status": "active"})

    today = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_decodes = await logs_col.count_documents({"request_time": {"$gte": today}})

    bot_statuses = []
    for name, health in metrics.bots.items():
        bot_statuses.append(
            {
                "name": name,
                "is_running": health.is_running,
                "total_processed": health.total_processed,
                "total_errors": health.total_errors,
            }
        )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "total_users": total_users,
            "total_files": total_files,
            "active_files": active_files,
            "today_decodes": today_decodes,
            "bot_statuses": bot_statuses,
            "send_success": metrics.send_success_count,
            "send_fail": metrics.send_fail_count,
            "backup_count": metrics.backup_count,
            "backup_fail": metrics.backup_fail_count,
        },
    )


@app.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    admin=Depends(verify_admin),
    page: int = Query(1, ge=1),
    search: str = Query(""),
):
    per_page = 20
    users_col = get_users_col()

    query = {}
    if search:
        if search.isdigit():
            query["user_id"] = int(search)
        else:
            query["$or"] = [
                {"username": {"$regex": search, "$options": "i"}},
                {"first_name": {"$regex": search, "$options": "i"}},
            ]

    total = await users_col.count_documents(query)
    skip = (page - 1) * per_page
    users = await users_col.find(query, sort=("created_at", -1), skip=skip, limit=per_page)

    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "users": users,
            "page": page,
            "total": total,
            "per_page": per_page,
            "search": search,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        },
    )


@app.post("/users/{user_id}/membership")
async def update_membership(
    user_id: int,
    level: str = Query(...),
    admin=Depends(verify_admin),
):
    if level not in ("free", "basic", "premium"):
        raise HTTPException(status_code=400, detail="无效的会员等级")

    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    update = {
        "$set": {
            "membership_level": level,
            "updated_at": datetime.datetime.utcnow(),
        }
    }
    if level == "free":
        update["$set"]["daily_decode_quota"] = settings.FREE_DAILY_QUOTA
        update["$set"]["can_upload"] = False
        update["$set"]["external_decode_quota"] = settings.FREE_EXTERNAL_DAILY_QUOTA
        update["$set"]["external_used_today"] = 0
    elif level == "basic":
        update["$set"]["daily_decode_quota"] = settings.BASIC_DAILY_QUOTA
        update["$set"]["can_upload"] = True
        update["$set"]["external_decode_quota"] = settings.BASIC_EXTERNAL_DAILY_QUOTA
        update["$set"]["external_used_today"] = 0
    elif level == "premium":
        update["$set"]["daily_decode_quota"] = settings.PREMIUM_DAILY_QUOTA
        update["$set"]["can_upload"] = True
        update["$set"]["external_decode_quota"] = settings.PREMIUM_EXTERNAL_DAILY_QUOTA
        update["$set"]["external_used_today"] = 0

    await users_col.update_one({"user_id": user_id}, update)
    return RedirectResponse(url="/users", status_code=303)


@app.post("/users/{user_id}/toggle_ban")
async def toggle_ban(user_id: int, admin=Depends(verify_admin)):
    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    new_ban = not user.get("is_banned", False)
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": new_ban, "updated_at": datetime.datetime.utcnow()}},
    )
    return RedirectResponse(url="/users", status_code=303)


@app.get("/files", response_class=HTMLResponse)
async def files_page(
    request: Request,
    admin=Depends(verify_admin),
    page: int = Query(1, ge=1),
    search: str = Query(""),
):
    per_page = 20
    files_col = get_file_records_col()

    query = {}
    if search:
        if search.isdigit():
            query["uploader_id"] = int(search)
        else:
            query["file_code"] = {"$regex": search, "$options": "i"}

    total = await files_col.count_documents(query)
    skip = (page - 1) * per_page
    files = await files_col.find(query, sort=("create_time", -1), skip=skip, limit=per_page)

    return templates.TemplateResponse(
        "files.html",
        {
            "request": request,
            "files": files,
            "page": page,
            "total": total,
            "per_page": per_page,
            "search": search,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        },
    )


@app.post("/files/{file_code}/delete")
async def delete_file(file_code: str, admin=Depends(verify_admin)):
    files_col = get_file_records_col()
    result = await files_col.update_one(
        {"file_code": file_code},
        {"$set": {"status": "deleted"}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="文件不存在")
    return RedirectResponse(url="/files", status_code=303)


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    admin=Depends(verify_admin),
    page: int = Query(1, ge=1),
):
    per_page = 50
    logs_col = get_decode_logs_col()

    total = await logs_col.count_documents({})
    skip = (page - 1) * per_page
    logs = await logs_col.find(sort=("request_time", -1), skip=skip, limit=per_page)

    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "logs": logs,
            "page": page,
            "total": total,
            "per_page": per_page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        },
    )


@app.get("/health", response_class=HTMLResponse)
async def health_page(request: Request, admin=Depends(verify_admin)):
    bot_statuses = []
    for name, health in metrics.bots.items():
        bot_statuses.append(
            {
                "name": name,
                "is_running": health.is_running,
                "last_ping": (
                    datetime.datetime.fromtimestamp(health.last_ping).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    if health.last_ping
                    else "N/A"
                ),
                "total_processed": health.total_processed,
                "total_errors": health.total_errors,
            }
        )
    return templates.TemplateResponse(
        "health.html",
        {
            "request": request,
            "bot_statuses": bot_statuses,
        },
    )