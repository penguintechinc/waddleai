"""
WaddleAI Management API v1 - Usage Tracking Endpoints
"""

import asyncio
import csv
import io
from datetime import date, datetime, timedelta

from quart import Response, g, jsonify, request

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_role


@api_v1_bp.route("/usage/summary", methods=["GET"])
@require_auth
async def get_usage_summary():
    """Get usage summary (daily/monthly)"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    today = date.today()
    month_start = today.replace(day=1)

    def _fetch():
        # Build query based on role
        if user_role == "admin":
            daily_query = db.token_usage.date == today
            monthly_query = db.token_usage.date >= month_start
        elif user_role in ["resource_manager", "reporter"]:
            daily_query = (db.token_usage.date == today) & (db.token_usage.organization_id == org_id)
            monthly_query = (db.token_usage.date >= month_start) & (db.token_usage.organization_id == org_id)
        else:
            daily_query = (db.token_usage.date == today) & (db.token_usage.user_id == user_id)
            monthly_query = (db.token_usage.date >= month_start) & (db.token_usage.user_id == user_id)

        return db(daily_query).select(), db(monthly_query).select()

    daily_records, monthly_records = await asyncio.to_thread(_fetch)

    return jsonify(
        {
            "summary": {
                "daily": {
                    "date": today.isoformat(),
                    "waddleai_tokens": sum(r.waddleai_tokens or 0 for r in daily_records),
                    "tokens_input": sum(r.tokens_input_total or 0 for r in daily_records),
                    "tokens_output": sum(r.tokens_output_total or 0 for r in daily_records),
                    "requests": sum(r.request_count or 0 for r in daily_records),
                    "cost_usd": sum(r.cost_usd_total or 0 for r in daily_records),
                },
                "monthly": {
                    "month": month_start.isoformat(),
                    "waddleai_tokens": sum(r.waddleai_tokens or 0 for r in monthly_records),
                    "tokens_input": sum(r.tokens_input_total or 0 for r in monthly_records),
                    "tokens_output": sum(r.tokens_output_total or 0 for r in monthly_records),
                    "requests": sum(r.request_count or 0 for r in monthly_records),
                    "cost_usd": sum(r.cost_usd_total or 0 for r in monthly_records),
                },
            }
        }
    )


@api_v1_bp.route("/usage/by-model", methods=["GET"])
@require_auth
async def get_usage_by_model():
    """Get usage breakdown by model"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    days = request.args.get("days", 30, type=int)
    start_date = date.today() - timedelta(days=days)

    def _fetch():
        # Build query based on role
        if user_role == "admin":
            base_query = db.usage_logs.timestamp >= datetime.combine(start_date, datetime.min.time())
        elif user_role in ["resource_manager", "reporter"]:
            base_query = (db.usage_logs.timestamp >= datetime.combine(start_date, datetime.min.time())) & (
                db.usage_logs.organization_id == org_id
            )
        else:
            base_query = (db.usage_logs.timestamp >= datetime.combine(start_date, datetime.min.time())) & (
                db.usage_logs.user_id == user_id
            )

        return db(base_query).select()

    records = await asyncio.to_thread(_fetch)

    # Group by model
    model_usage = {}
    for record in records:
        model = record.model_used or "unknown"
        if model not in model_usage:
            model_usage[model] = {"tokens": 0, "requests": 0, "cost_usd": 0}
        model_usage[model]["tokens"] += record.waddleai_tokens_used or 0
        model_usage[model]["requests"] += 1
        model_usage[model]["cost_usd"] += record.cost_estimate_usd or 0

    return jsonify({"period_days": days, "by_model": model_usage})


@api_v1_bp.route("/usage/by-provider", methods=["GET"])
@require_auth
async def get_usage_by_provider():
    """Get usage breakdown by provider"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    days = request.args.get("days", 30, type=int)
    start_date = date.today() - timedelta(days=days)

    def _fetch():
        # Build query based on role
        if user_role == "admin":
            base_query = db.usage_logs.timestamp >= datetime.combine(start_date, datetime.min.time())
        elif user_role in ["resource_manager", "reporter"]:
            base_query = (db.usage_logs.timestamp >= datetime.combine(start_date, datetime.min.time())) & (
                db.usage_logs.organization_id == org_id
            )
        else:
            base_query = (db.usage_logs.timestamp >= datetime.combine(start_date, datetime.min.time())) & (
                db.usage_logs.user_id == user_id
            )

        return db(base_query).select()

    records = await asyncio.to_thread(_fetch)

    # Group by provider
    provider_usage = {}
    for record in records:
        provider = record.provider_type or "unknown"
        if provider not in provider_usage:
            provider_usage[provider] = {
                "tokens": 0,
                "tokens_input": 0,
                "tokens_output": 0,
                "requests": 0,
                "cost_usd": 0,
                "avg_latency_ms": 0,
            }
        provider_usage[provider]["tokens"] += record.waddleai_tokens_used or 0
        provider_usage[provider]["tokens_input"] += record.llm_tokens_input or 0
        provider_usage[provider]["tokens_output"] += record.llm_tokens_output or 0
        provider_usage[provider]["requests"] += 1
        provider_usage[provider]["cost_usd"] += record.cost_estimate_usd or 0

    # Calculate averages
    for provider, data in provider_usage.items():
        if data["requests"] > 0:
            data["avg_latency_ms"] = data.get("total_latency", 0) / data["requests"]

    return jsonify({"period_days": days, "by_provider": provider_usage})


@api_v1_bp.route("/usage/by-user", methods=["GET"])
@require_auth
@require_role("admin", "resource_manager")
async def get_usage_by_user():
    """Get usage breakdown by user"""
    user_role = g.user.get("role")
    org_id = g.user.get("organization_id")

    days = request.args.get("days", 30, type=int)
    start_date = date.today() - timedelta(days=days)

    def _fetch():
        # Build query based on role
        if user_role == "admin":
            base_query = db.token_usage.date >= start_date
        else:
            base_query = (db.token_usage.date >= start_date) & (db.token_usage.organization_id == org_id)

        records = db(base_query).select()

        # Group by user
        user_usage = {}
        for record in records:
            uid = record.user_id
            if uid not in user_usage:
                user = db(db.users.id == uid).select().first()
                user_usage[uid] = {
                    "user_id": uid,
                    "username": user.username if user else "unknown",
                    "tokens": 0,
                    "requests": 0,
                    "cost_usd": 0,
                }
            user_usage[uid]["tokens"] += record.waddleai_tokens or 0
            user_usage[uid]["requests"] += record.request_count or 0
            user_usage[uid]["cost_usd"] += record.cost_usd_total or 0

        return user_usage

    user_usage = await asyncio.to_thread(_fetch)

    return jsonify({"period_days": days, "by_user": list(user_usage.values())})


@api_v1_bp.route("/usage/by-key", methods=["GET"])
@require_auth
async def get_usage_by_key():
    """Get usage breakdown by API key"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    days = request.args.get("days", 30, type=int)
    start_date = date.today() - timedelta(days=days)

    def _fetch():
        # Build query based on role
        if user_role == "admin":
            base_query = db.token_usage.date >= start_date
        elif user_role in ["resource_manager", "reporter"]:
            base_query = (db.token_usage.date >= start_date) & (db.token_usage.organization_id == org_id)
        else:
            base_query = (db.token_usage.date >= start_date) & (db.token_usage.user_id == user_id)

        records = db(base_query).select()

        # Group by key
        key_usage = {}
        for record in records:
            kid = record.virtual_key_id
            if kid and kid not in key_usage:
                key = db(db.virtual_keys.id == kid).select().first()
                key_usage[kid] = {
                    "key_id": kid,
                    "key_name": key.name if key else "unknown",
                    "key_prefix": key.key_prefix if key else "unknown",
                    "tokens": 0,
                    "requests": 0,
                    "cost_usd": 0,
                }
            if kid:
                key_usage[kid]["tokens"] += record.waddleai_tokens or 0
                key_usage[kid]["requests"] += record.request_count or 0
                key_usage[kid]["cost_usd"] += record.cost_usd_total or 0

        return key_usage

    key_usage = await asyncio.to_thread(_fetch)

    return jsonify({"period_days": days, "by_key": list(key_usage.values())})


@api_v1_bp.route("/usage/cost", methods=["GET"])
@require_auth
async def get_cost_analytics():
    """Get cost analytics"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    days = request.args.get("days", 30, type=int)
    start_date = date.today() - timedelta(days=days)

    def _fetch():
        # Build query based on role
        if user_role == "admin":
            base_query = db.token_usage.date >= start_date
        elif user_role in ["resource_manager", "reporter"]:
            base_query = (db.token_usage.date >= start_date) & (db.token_usage.organization_id == org_id)
        else:
            base_query = (db.token_usage.date >= start_date) & (db.token_usage.user_id == user_id)

        return db(base_query).select(orderby=db.token_usage.date)

    records = await asyncio.to_thread(_fetch)

    # Daily cost breakdown
    daily_cost = {}
    for record in records:
        day = record.date.isoformat()
        if day not in daily_cost:
            daily_cost[day] = 0
        daily_cost[day] += record.cost_usd_total or 0

    # Calculate totals and averages
    total_cost = sum(daily_cost.values())
    avg_daily_cost = total_cost / days if days > 0 else 0
    projected_monthly_cost = avg_daily_cost * 30

    return jsonify(
        {
            "period_days": days,
            "total_cost_usd": round(total_cost, 4),
            "avg_daily_cost_usd": round(avg_daily_cost, 4),
            "projected_monthly_cost_usd": round(projected_monthly_cost, 4),
            "daily_cost": daily_cost,
        }
    )


@api_v1_bp.route("/usage/export", methods=["GET"])
@require_auth
async def export_usage():
    """Export usage data (CSV/JSON)"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    format_type = request.args.get("format", "json")
    days = request.args.get("days", 30, type=int)
    start_date = date.today() - timedelta(days=days)

    def _fetch():
        # Build query based on role
        if user_role == "admin":
            base_query = db.token_usage.date >= start_date
        elif user_role in ["resource_manager", "reporter"]:
            base_query = (db.token_usage.date >= start_date) & (db.token_usage.organization_id == org_id)
        else:
            base_query = (db.token_usage.date >= start_date) & (db.token_usage.user_id == user_id)

        return db(base_query).select(orderby=db.token_usage.date)

    records = await asyncio.to_thread(_fetch)

    data = []
    for record in records:
        data.append(
            {
                "date": record.date.isoformat(),
                "user_id": record.user_id,
                "organization_id": record.organization_id,
                "virtual_key_id": record.virtual_key_id,
                "waddleai_tokens": record.waddleai_tokens,
                "tokens_input": record.tokens_input_total,
                "tokens_output": record.tokens_output_total,
                "request_count": record.request_count,
                "cost_usd": record.cost_usd_total,
            }
        )

    if format_type == "csv":
        if not data:
            return Response("", mimetype="text/csv")

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)

        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=usage-export-{date.today().isoformat()}.csv"},
        )

    return jsonify({"data": data, "count": len(data)})
