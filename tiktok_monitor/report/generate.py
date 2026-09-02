"""Orchestration: aggregate -> prompt -> Claude -> render -> save."""
import logging
import sqlite3

from .. import db
from . import claude as claude_module
from . import data as data_module
from . import prompt as prompt_module
from . import render as render_module

logger = logging.getLogger(__name__)


def generate_report(conn: sqlite3.Connection, live_session_id: int) -> int:
    aggregated = data_module.aggregate_session_data(conn, live_session_id)

    stats = aggregated["comment_curation_stats"]
    logger.info(
        "comment curation (session=%s): total=%s representative=%s noise_excluded=%s "
        "category_total=%s category_representative=%s",
        live_session_id,
        stats["total_comments"],
        stats["representative_count"],
        stats["noise_excluded"],
        stats["category_counts_total"],
        stats["category_counts_representative"],
    )

    system_prompt = prompt_module.build_system_prompt()
    user_content = prompt_module.build_user_content(aggregated)
    ai_sections, usage = claude_module.generate_sections(system_prompt, user_content)
    used_winning_patterns = prompt_module.load_winning_patterns() is not None
    summary_json, recommendation_md = render_module.render_report(
        aggregated,
        ai_sections,
        used_winning_patterns=used_winning_patterns,
        usage=usage,
        comment_curation_stats=stats,
    )
    return db.insert_report(conn, live_session_id, summary_json, recommendation_md)
