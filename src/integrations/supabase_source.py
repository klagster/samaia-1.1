from typing import Any, Dict, Iterable, Optional, List
import os
import logging
from supabase import create_client, Client
from itertools import islice

def _wrap_with_logging(name: str, iterable: Iterable[Dict[str, Any]], preview: int = 1, maxlen: int = 600) -> Iterable[Dict[str, Any]]:
    """
    Wrap a row-iterable to log a small preview and a final row count
    without materializing the full result in memory.
    """
    def _gen():
        count = 0
        for row in iterable:
            if count < preview:
                # compact preview string
                try:
                    s = str(row)
                except Exception:
                    s = f"<unprintable {type(row)}>"
                if len(s) > maxlen:
                    s = s[:maxlen] + "…"
                logging.info(f"[Supabase Source] {name} head[{count}]: {s}")
            count += 1
            yield row
        logging.info(f"[Supabase Source] {name} total_rows={count}")
    return _gen()

def _create_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY in environment")
    return create_client(url, key)

def _apply_filters(query, filters: Dict[str, str]):
    if not filters:
        return query
    for col, spec in filters.items():
        if not isinstance(spec, str) or "." not in spec:
            query = query.eq(col, spec)
            continue
        op, val = spec.split(".", 1)
        if op == "eq":
            query = query.eq(col, val)
        elif op == "ilike":
            query = query.ilike(col, val)
        elif op == "gt":
            query = query.gt(col, val)
        elif op == "gte":
            query = query.gte(col, val)
        elif op == "lt":
            query = query.lt(col, val)
        elif op == "lte":
            query = query.lte(col, val)
        else:
            query = query.eq(col, val)
    return query

def _apply_order(query, order: Optional[str]):
    if not order:
        return query
    if "." in order:
        col, direction = order.split(".", 1)
        desc = direction.lower() == "desc"
        return query.order(col, desc=desc)
    return query.order(order, desc=False)

def _stream_rows(
    supabase: Client,
    table: str,
    select: str,
    filters: Optional[Dict[str, str]] = None,
    order: Optional[str] = None,
    page_size: int = 1000,
) -> Iterable[Dict[str, Any]]:
    start = 0
    while True:
        end = start + page_size - 1
        query = supabase.table(table).select(select)
        query = _apply_filters(query, filters or {})
        query = _apply_order(query, order)
        resp = query.range(start, end).execute()
        rows = resp.data or []
        for r in rows:
            yield r
        if len(rows) < page_size:
            break
        start += page_size

def _stream_data_acquisition(
    supabase: Client,
    campaign_id: str,
    target_account_id: str,
    page_size: int = 1000,
) -> Iterable[Dict[str, Any]]:
    """Stream `needs_analysis_output` rows for the campaign's data acquisition step.

    NOTE: `needs_analysis_output` does not store target-account fields. We therefore
    ignore `target_account_id` here and fetch strictly by (campaign_id, step_name).
    """
    start = 0
    while True:
        end = start + page_size - 1
        q = (
            supabase.table("needs_analysis_output")
            .select("step_name,output_data,created_at,updated_at")
            .eq("campaign_id", campaign_id)
            .eq("step_name", "data_acquisition")
            .range(start, end)
        )
        resp = q.execute()
        rows = resp.data or []
        for r in rows:
            yield r
        if len(rows) < page_size:
            break
        start += page_size

def stream_campaign_datasets(
    campaign_id: str,
    target_account_id: str,
    page_size: int = 5000,
) -> Dict[str, Iterable[Dict[str, Any]]]:
    logging.basicConfig(level=logging.INFO)
    logging.info(f"[Supabase Source] Streaming datasets for campaign_id={campaign_id}, target_account_id={target_account_id}, page_size={page_size}")

    supabase = _create_client()

    # Fetch exactly one target account row as a dict (not a generator/list)
    ta_iter = _stream_rows(
        supabase,
        table="target_accounts",
        select=(
            "id,campaign_id,"
            "account_name,"
            "company_size,company_website,industry,location,"
            "contact_person,contact_email,"
            "status,priority,notes"
        ),
        filters={
            "campaign_id": f"eq.{campaign_id}",
            "id": f"eq.{target_account_id}",
        },
        order=None,
        page_size=page_size,
    )
    # Materialize the first row
    ta_first = next(ta_iter, None)
    if ta_first is not None:
        # Log a compact preview of the single row for consistency with other streams
        try:
            s = str(ta_first)
        except Exception:
            s = f"<unprintable {type(ta_first)}>"
        if len(s) > 600:
            s = s[:600] + "…"
        logging.info(f"[Supabase Source] target_account head[0]: {s}")
        logging.info(f"[Supabase Source] target_account total_rows=1")
    else:
        logging.info(f"[Supabase Source] target_account head[0]: <none>")
        logging.info(f"[Supabase Source] target_account total_rows=0")

    datasets = {
        "campaign": _wrap_with_logging(
            "campaign",
            _stream_rows(
                supabase,
                table="campaigns",
                select=(
                    "id,name,description,status,start_date,end_date,budget,client_id,"
                    "client_profile,target_industry_segments,geo_focus,target_personas,"
                    "known_competitors,campaign_value_prop,lookback_period,"
                    "clients ( company, homepage, industry, name )"
                ),
                filters={"id": f"eq.{campaign_id}"},
                order=None,
                page_size=page_size,
            ),
            preview=1,
        ),

        # FIXED: Wrap single dict in a list so list() doesn't convert it to keys
        "target_account": [ta_first] if ta_first is not None else [],

        "client_profile": _wrap_with_logging(
            "client_profile",
            _stream_rows(
                supabase,
                table="needs_analysis_output",
                select="step_name,output_data",
                filters={"campaign_id": f"eq.{campaign_id}", "step_name": "eq.client_profile"},
                order=None,
                page_size=page_size,
            ),
            preview=1,
        ),

        "customer_challenges": _wrap_with_logging(
            "customer_challenges",
            _stream_rows(
                supabase,
                table="needs_analysis_output",
                select="step_name,output_data",
                filters={"campaign_id": f"eq.{campaign_id}", "step_name": "eq.customer_challenges"},
                order=None,
                page_size=page_size,
            ),
            preview=1,
        ),

        "data_signal_mapping": _wrap_with_logging(
            "data_signal_mapping",
            _stream_rows(
                supabase,
                table="needs_analysis_output",
                select="step_name,output_data",
                filters={"campaign_id": f"eq.{campaign_id}", "step_name": "eq.data_signal_mapping"},
                order=None,
                page_size=page_size,
            ),
            preview=1,
        ),

        "campaign_signals": _wrap_with_logging(
            "campaign_signals",
            _stream_rows(
                supabase,
                table="needs_analysis_output",
                select="step_name,output_data",
                filters={"campaign_id": f"eq.{campaign_id}", "step_name": "eq.data_signal_mapping"},
                order=None,
                page_size=page_size,
            ),
            preview=1,
        ),

        "data_acquisition": _wrap_with_logging(
            "data_acquisition",
            _stream_data_acquisition(
                supabase,
                campaign_id=campaign_id,
                target_account_id=target_account_id,
                page_size=page_size,
            ),
            preview=1,
        ),
    }

    logging.info(f"[Supabase Source] Datasets prepared with keys: {list(datasets.keys())}")
    return datasets

def stream_target_account(target_account_id: str, page_size: int = 5000) -> Dict[str, Iterable[Dict[str, Any]]]:
    logging.info(f"[Supabase Source] stream_target_account: target_account_id={target_account_id}, page_size={page_size}")
    supabase = _create_client()
    stream = _stream_rows(
        supabase,
        table="target_accounts",
        select="id,campaign_id,account_name",
        filters={"id": f"eq.{target_account_id}"},
        order=None,
        page_size=page_size,
    )
    return {"target_account": stream}


def list_target_accounts_for_campaign(campaign_id: str, page_size: int = 5000) -> List[Dict[str, Any]]:
    """Return all target accounts for a given campaign as a materialized list.

    This is used by GCF to fan out and run the pipeline for each target account
    when only a campaign_id is provided. We intentionally materialize the list
    instead of returning a generator so the caller can safely iterate multiple
    times and log counts without re-querying Supabase.
    """
    logging.info(
        f"[Supabase Source] list_target_accounts_for_campaign: campaign_id={campaign_id}, page_size={page_size}"
    )
    supabase = _create_client()

    rows: List[Dict[str, Any]] = []
    # Keep the select aligned with stream_campaign_datasets target_account fields
    for r in _stream_rows(
        supabase,
        table="target_accounts",
        select=(
            "id,campaign_id,"
            "account_name,"
            "company_size,company_website,industry,location,"
            "contact_person,contact_email,"
            "status,priority,notes"
        ),
        filters={"campaign_id": f"eq.{campaign_id}"},
        order="priority.desc",
        page_size=page_size,
    ):
        rows.append(r)

    logging.info(
        f"[Supabase Source] list_target_accounts_for_campaign: fetched {len(rows)} target_accounts"
    )
    return rows