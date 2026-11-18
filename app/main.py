from pathlib import Path

from blacksheep import Application, Content, Cookie, Request, Response
from blacksheep.server.responses import redirect
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import get_db, init_db
from app.services import (
    correct_header,
    gap_cards,
    get_batch,
    list_batches,
    list_headers,
    list_replay_logs,
    list_records,
    list_reports,
    list_templates,
    open_snapshot,
    replay_record,
    run_gap_report,
    stamp_batch,
    trace_downstream,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-watermark-session")

app = Application()
app.serve_files(str(STATIC), root_path="/static")


@app.on_start
async def _on_start(_app) -> None:
    init_db()


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("watermark_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_response(data: dict, status: int = 200) -> Response:
    import json

    return Response(
        status,
        content=Content(
            b"application/json",
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ),
    )


def html_response(path: Path) -> Response:
    return Response(
        200,
        content=Content(b"text/html; charset=utf-8", path.read_bytes()),
    )


@app.router.get("/")
async def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return redirect("/app/")
    return redirect("/login")


@app.router.get("/login")
async def login_page():
    return html_response(STATIC / "login.html")


@app.router.get("/app/")
async def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return redirect("/login")
    return html_response(STATIC / "index.html")


@app.router.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_response({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_response({"ok": True, "user": user})
    resp.set_cookie(
        Cookie(
            "watermark_session",
            serializer.dumps({"user": user}),
            http_only=True,
            path="/",
        )
    )
    return resp


@app.router.post("/api/logout")
async def api_logout():
    resp = json_response({"ok": True})
    resp.unset_cookie("watermark_session")
    return resp


@app.router.get("/api/meta")
async def api_meta(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(
        {
            "lab_name": Config.LAB_NAME,
            "product_line": "cc-watermark-lab",
            "chain": ["水印模板", "批次盖印", "下游追索", "缺口对账", "水印核对"],
        }
    )


@app.router.get("/api/templates")
async def api_templates(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_templates(status=status_val)})


@app.router.get("/api/batches")
async def api_batches(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_batches(status=status_val)})


@app.router.get("/api/batches/{batch_id}")
async def api_batch_detail(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    batch_id = int(request.route_values["batch_id"])
    batch = get_batch(batch_id)
    if not batch:
        return json_response({"error": "批次不存在"}, 404)
    # 批次详情：同查所属批头栏的头栏数/实数/漂移差量
    header_info = None
    with get_db() as conn:
        h = conn.execute(
            "SELECT * FROM batch_headers WHERE biz_date=? AND table_name=?",
            (batch["biz_date"], batch["table_name"]),
        ).fetchone()
        if h:
            actual = conn.execute(
                "SELECT COUNT(*) AS c FROM run_batches WHERE biz_date=? "
                "AND table_name=? AND status='已盖印'",
                (batch["biz_date"], batch["table_name"]),
            ).fetchone()["c"]
            header_info = {
                "header_id": h["id"],
                "declared_stamped": h["declared_stamped"],
                "actual_stamped": actual,
                "delta": int(h["declared_stamped"]) - int(actual),
                "drifted": int(h["declared_stamped"]) != int(actual),
                "version_no": h["version_no"],
            }
    return json_response({"batch": batch, "header": header_info})


@app.router.post("/api/batches/{batch_id}/stamp")
async def api_stamp(request: Request):
    try:
        require_user(request)
        batch_id = int(request.route_values["batch_id"])
        body = await request.json()
        batch = stamp_batch(batch_id, str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "batch": batch})


@app.router.post("/api/trace")
async def api_trace(request: Request):
    try:
        require_user(request)
        body = await request.json()
        result = trace_downstream(str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, **result})


@app.router.get("/api/gap-reports")
async def api_gap_list(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_reports()})


@app.router.post("/api/gap-reports")
async def api_gap_run(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    body = await request.json()
    report = run_gap_report(str(body.get("as_of_date", "2026-11-16")))
    return json_response({"ok": True, "report": report})


# ---------------- 水印核对台 ----------------


@app.router.get("/api/reconcile/headers")
async def api_reconcile_headers(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_headers()})


@app.router.post("/api/reconcile/headers/{header_id}/snapshot")
async def api_reconcile_snapshot(request: Request):
    try:
        user = require_user(request)
        header_id = int(request.route_values["header_id"])
        snapshot = open_snapshot(header_id)
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "snapshot": snapshot, "operator": user})


@app.router.post("/api/reconcile/headers/{header_id}/correct")
async def api_reconcile_correct(request: Request):
    try:
        user = require_user(request)
        header_id = int(request.route_values["header_id"])
        body = await request.json()
        mode = str(body.get("mode", "")).strip()
        snapshot_id = body.get("snapshot_id")
        result = correct_header(
            header_id,
            mode,
            operator=user,
            snapshot_id=int(snapshot_id) if snapshot_id is not None else None,
        )
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "result": result})


@app.router.get("/api/reconcile/records")
async def api_reconcile_records(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_records()})


@app.router.post("/api/reconcile/records/{record_id}/replay")
async def api_reconcile_replay(request: Request):
    try:
        require_user(request)
        record_id = int(request.route_values["record_id"])
        result = replay_record(record_id)
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "replay": result})


@app.router.get("/api/reconcile/replay-logs")
async def api_reconcile_logs(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_replay_logs()})


@app.router.get("/api/reconcile/gap-cards")
async def api_reconcile_gap_cards(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": gap_cards()})
