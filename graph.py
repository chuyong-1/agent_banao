"""
graph.py — LangGraph Pipeline Construction
==========================================
Wires the four node functions into a compiled StateGraph.

Pipeline (linear, no branches):

    ┌───────────────────┐
    │  extract_intent   │  parse raw text → structured requirements
    └────────┬──────────┘
             │
    ┌────────▼──────────┐
    │  ingest_erp_data  │  load HR / ERP data
    └────────┬──────────┘
             │
    ┌────────▼──────────┐
    │  compute_metrics  │  availability scores per employee
    └────────┬──────────┘
             │
    ┌────────▼──────────┐
    │    matchmaker     │  rank & score candidates → final payload
    └───────────────────┘

The compiled `app` object is importable and can be invoked with:
    result = app.invoke({"raw_project_input": "...", "errors": []})
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from state import GraphState
from nodes import (
    compute_metrics_node,
    extract_intent_node,
    ingest_erp_data_node,
    matchmaker_node,
)

logger = logging.getLogger(__name__)


def build_graph() -> StateGraph:
    """
    Instantiate and wire the StateGraph.
    Returns the *uncompiled* graph (useful for visualisation / testing).
    """
    graph = StateGraph(GraphState)

    # ── Register nodes ────────────────────────────────────────────────────
    graph.add_node("extract_intent",  extract_intent_node)
    graph.add_node("ingest_erp_data", ingest_erp_data_node)
    graph.add_node("compute_metrics", compute_metrics_node)
    graph.add_node("matchmaker",      matchmaker_node)

    # ── Wire edges (linear flow) ──────────────────────────────────────────
    graph.add_edge(START,            "extract_intent")
    graph.add_edge("extract_intent", "ingest_erp_data")
    graph.add_edge("ingest_erp_data","compute_metrics")
    graph.add_edge("compute_metrics","matchmaker")
    graph.add_edge("matchmaker",      END)

    return graph


def compile_graph():
    """
    Build and compile the graph into a runnable LangGraph app.
    The compiled app exposes `.invoke()`, `.stream()`, and `.astream()`.
    """
    graph = build_graph()
    app = graph.compile()
    logger.info("LangGraph pipeline compiled successfully.")
    return app


# Module-level compiled app — imported by server.py
app = compile_graph()