import os
from flask import Blueprint, render_template, jsonify
from app.blueprints.auth.routes import login_required
from app.services.jira_service import fetch_jira_data

board_bp = Blueprint("board", __name__)


@board_bp.route("/")
@login_required
def home():
    candidates_data, done_data, ready_prod_data, chart_data, candidates_chart_data, pending_ta = fetch_jira_data()

    return render_template(
        "index.html",
        candidates=candidates_data,
        ready_prod_tickets=ready_prod_data,
        done_tickets=done_data,
        chart_data=chart_data,
        candidates_chart_data=candidates_chart_data,
        pending_ta_tickets=pending_ta,
        jira_url=os.getenv("JIRA_URL") or "https://atlassian.net"
    )


@board_bp.route("/api/refresh", methods=["GET"])
@login_required
def api_refresh():
    candidates_data, done_data, ready_prod_data, chart_data, candidates_chart_data, pending_ta = fetch_jira_data()
    return jsonify({
        "success": True,
        "candidates": candidates_data,
        "ready_prod_tickets": ready_prod_data,
        "done_tickets": done_data,
        "chart_data": chart_data,
        "candidates_chart_data": candidates_chart_data,
        "pending_ta_tickets": pending_ta
    })
