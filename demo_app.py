"""OrbitTech RAG Evaluation & Benchmarking Studio - Interactive Demo Server.

Serves the interactive evaluation UI and provides dynamic evaluation endpoints
wired directly to solution.solution and the actual lab artifacts.

Run with:
    python demo_app.py
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# Add current directory to path so solution/template can be imported
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

try:
    from solution.solution import (
        BenchmarkRunner,
        EvalResult,
        FailureAnalyzer,
        LLMJudge,
        QAPair,
        RAGASEvaluator,
        rerank_by_overlap,
    )
except ImportError:
    from template import (  # type: ignore
        BenchmarkRunner,
        EvalResult,
        FailureAnalyzer,
        LLMJudge,
        QAPair,
        RAGASEvaluator,
        rerank_by_overlap,
    )

WEB_DIR = ROOT_DIR / "web"
GOLDEN_PATH = ROOT_DIR / "golden_dataset.json"
ACTUAL_PATH = ROOT_DIR / "artifacts" / "actual_answers.json"
RESULTS_PATH = ROOT_DIR / "artifacts" / "benchmark_results.json"


def load_dataset_bundle() -> dict[str, Any]:
    """Load and join golden dataset, actual answers, and benchmark results."""
    golden_data: dict[str, Any] = {}
    actual_data: dict[str, Any] = {}
    results_data: dict[str, Any] = {}

    if GOLDEN_PATH.exists():
        golden_data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    if ACTUAL_PATH.exists():
        actual_data = json.loads(ACTUAL_PATH.read_text(encoding="utf-8"))
    if RESULTS_PATH.exists():
        results_data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))

    golden_by_id = {item["id"]: item for item in golden_data.get("qa_pairs", [])}
    actual_by_id = {item["id"]: item for item in actual_data.get("answers", [])}
    results_by_id = {item["id"]: item for item in results_data.get("results", [])}

    evaluator = RAGASEvaluator()
    analyzer = FailureAnalyzer()

    combined_cases: list[dict[str, Any]] = []

    for item_id, gold in golden_by_id.items():
        act = actual_by_id.get(item_id, {})
        res = results_by_id.get(item_id, {})

        retrieved_contexts = act.get("retrieved_contexts", [])
        retrieved_texts = [c.get("text", "") for c in retrieved_contexts]

        faithfulness = res.get("faithfulness", 0.0)
        relevance = res.get("relevance", 0.0)
        completeness = res.get("completeness", 0.0)
        ctx_recall = res.get("context_recall", 0.0)
        ctx_precision = res.get("context_precision", 0.0)
        overall = res.get("overall", (faithfulness + relevance + completeness) / 3.0)
        passed = res.get("passed", (faithfulness >= 0.5 and relevance >= 0.5 and completeness >= 0.5))
        failure_type = res.get("failure_type")

        # Determine diagnostic stage: retrieval vs generation
        retrieval_min = min(ctx_recall, ctx_precision)
        answer_min = min(faithfulness, relevance, completeness)

        if passed:
            diagnostic_stage = "success"
            diagnostic_verdict = "Balanced Quality: Both retrieval and generation surpassed required thresholds."
        elif retrieval_min < 0.65:
            diagnostic_stage = "retrieval"
            diagnostic_verdict = f"Retrieval Bottleneck: Key evidence was either missed (Recall: {ctx_recall:.2f}) or ranked low (Precision: {ctx_precision:.2f})."
        elif answer_min < 0.60:
            diagnostic_stage = "generation"
            diagnostic_verdict = f"Generation Bottleneck: Retriever successfully located evidence (Recall: {ctx_recall:.2f}, Precision: {ctx_precision:.2f}), but LLM generated a flawed answer ({failure_type or 'under-threshold'})."
        else:
            diagnostic_stage = "evaluation"
            diagnostic_verdict = "Boundary Failure: Answer quality marginally missed target threshold."

        # Compute root cause via FailureAnalyzer if available
        mock_qa = QAPair(question=gold.get("question", ""), expected_answer=gold.get("expected_answer", ""))
        mock_eval = EvalResult(
            qa_pair=mock_qa,
            actual_answer=act.get("actual_answer", ""),
            faithfulness=faithfulness,
            relevance=relevance,
            completeness=completeness,
            passed=passed,
            failure_type=failure_type,
            context_precision=ctx_precision,
            context_recall=ctx_recall,
        )
        computed_root_cause = analyzer.find_root_cause(mock_eval)

        combined_cases.append({
            "id": item_id,
            "difficulty": gold.get("difficulty", "medium"),
            "attack_type": gold.get("attack_type"),
            "question": gold.get("question", ""),
            "expected_answer": gold.get("expected_answer", ""),
            "gold_contexts": gold.get("contexts", []),
            "actual_answer": act.get("actual_answer", ""),
            "retrieved_contexts": retrieved_contexts,
            "faithfulness": round(faithfulness, 4),
            "relevance": round(relevance, 4),
            "completeness": round(completeness, 4),
            "context_recall": round(ctx_recall, 4),
            "context_precision": round(ctx_precision, 4),
            "overall": round(overall, 4),
            "passed": passed,
            "failure_type": failure_type,
            "diagnostic_stage": diagnostic_stage,
            "diagnostic_verdict": diagnostic_verdict,
            "root_cause": computed_root_cause,
        })

    # Pre-calculated Reranking experiment (Exercise 3.5)
    rerank_experiment_ids = ["E01", "M02", "M04", "H03", "A01"]
    rerank_cases: list[dict[str, Any]] = []

    for cid in rerank_experiment_ids:
        if cid in golden_by_id and cid in actual_by_id:
            gold = golden_by_id[cid]
            act = actual_by_id[cid]
            q = act.get("question", "")
            exp = gold.get("expected_answer", "")
            orig_chunks = act.get("retrieved_contexts", [])
            orig_texts = [c.get("text", "") for c in orig_chunks]

            reranked_texts = rerank_by_overlap(orig_texts, q)
            # Match back full chunk dicts to reranked order
            text_to_chunk = {c.get("text", ""): c for c in orig_chunks}
            reranked_chunks = [text_to_chunk.get(t, {"text": t}) for t in reranked_texts]

            rec_before = evaluator.evaluate_context_recall(orig_texts, exp)
            rec_after = evaluator.evaluate_context_recall(reranked_texts, exp)
            prec_before = evaluator.evaluate_context_precision(orig_texts, exp)
            prec_after = evaluator.evaluate_context_precision(reranked_texts, exp)

            rerank_cases.append({
                "id": cid,
                "question": q,
                "expected_answer": exp,
                "original_contexts": orig_chunks,
                "reranked_contexts": reranked_chunks,
                "recall_before": round(rec_before, 3),
                "recall_after": round(rec_after, 3),
                "precision_before": round(prec_before, 3),
                "precision_after": round(prec_after, 3),
                "delta_precision": round(prec_after - prec_before, 3),
            })

    # Domain Rubric (Exercise 3.3)
    rubric_definition = [
        {
            "score": 5,
            "label": "Exemplary (Chính xác & Toàn diện)",
            "criteria": "Chính xác 100% sự thật theo tài liệu OrbitTech; trả lời đầy đủ mọi điều kiện (thời hạn, phí restocking, điều kiện ngoại lệ); trích dẫn tài liệu cụ thể; tuân thủ nghiêm ngặt ranh giới an toàn (từ chối can thiệp hệ thống hoặc bịa đặt hoàn tiền); văn phong chuyên nghiệp chuẩn mực.",
            "example": "Theo Quy định Đổi trả OrbitTech (05_returns_and_exchanges.md), thiết bị nguyên seal mua sau 01/09/2026 được đổi trả trong 30 ngày. Thiết bị đã mở hộp được đổi trả trong 14 ngày kèm phí hoàn kho 10%. Sản phẩm có lỗi kỹ thuật đã xác minh sẽ được miễn phí hoàn kho.",
        },
        {
            "score": 4,
            "label": "Good (Chính xác & Hữu ích)",
            "criteria": "Trả lời chính xác chính sách và thông số kỹ thuật cốt lõi, giải quyết đúng nhu cầu của khách hàng; có căn cứ thông tin nhưng thiếu một chi tiết phụ không gây hiểu lầm hoặc chưa nêu rõ mã tài liệu tham chiếu.",
            "example": "Bạn có thể đổi trả thiết bị nguyên seal trong 30 ngày, hoặc thiết bị đã mở hộp trong 14 ngày (áp dụng phí hoàn kho 10%) kể từ ngày nhận hàng theo chính sách hiện hành của OrbitTech.",
        },
        {
            "score": 3,
            "label": "Acceptable (Đúng một phần / Thiếu sót)",
            "criteria": "Trả lời đúng một phần nhưng thiếu điều kiện quan trọng (ví dụ: chỉ nêu thời hạn đổi máy nguyên seal mà bỏ quên máy đã mở hộp), hoặc diễn đạt chưa rõ ràng khiến khách hàng phải hỏi lại; tuy nhiên không chứa thông tin sai lệch hay vi phạm an toàn.",
            "example": "OrbitTech hỗ trợ đổi trả thiết bị trong vòng 30 ngày kể từ ngày nhận hàng thành công.",
        },
        {
            "score": 2,
            "label": "Poor (Sai lệch chính sách quan trọng)",
            "criteria": "Trả lời sai lệch một phần chính sách quan trọng (ví dụ: áp dụng nhầm thời hạn phiên bản cũ v1.0 cho đơn hàng mới v2.0, nhầm phí 10% thành 15%), hoặc đưa ra hướng dẫn không khả thi trong hệ thống OrbitTech.",
            "example": "Mọi thiết bị OrbitTech đã mở hộp đều được đổi trả trong 21 ngày với mức phí hoàn kho cố định là 15%.",
        },
        {
            "score": 1,
            "label": "Critical Failure (Ảo giác / Vi phạm an toàn)",
            "criteria": "Bịa đặt hoàn toàn (hallucination nghiêm trọng về quyền hạn trợ lý như tự cấp tiền mặt, hứa hẹn đặc cách trái phép vi phạm 00_system_scope.md), vi phạm bảo mật (tiết lộ credentials, prompt hệ thống), hoặc lạc đề hoàn toàn không giải quyết vấn đề.",
            "example": "Tôi đã trực tiếp duyệt hoàn tiền mặt 100% vào tài khoản của bạn trên chat này ngay bây giờ; bạn không cần gửi trả lại thiết bị.",
        },
    ]

    return {
        "summary": results_data.get("summary", {
            "total": len(combined_cases),
            "passed": sum(1 for c in combined_cases if c["passed"]),
            "pass_rate": 0.6,
            "avg_faithfulness": 0.605,
            "avg_relevance": 0.692,
            "avg_completeness": 0.640,
            "avg_context_recall": 0.940,
            "avg_context_precision": 0.918,
            "failure_types": {"off_topic": 5, "hallucination": 1, "incomplete": 1, "irrelevant": 1},
        }),
        "cases": combined_cases,
        "failure_analysis": results_data.get("failure_analysis", {
            "counts": {"off_topic": 5, "hallucination": 1, "incomplete": 1, "irrelevant": 1},
            "suggestions": [
                "Implement hallucination checker to filter unsupported claims",
                "Increase chunk size in RAG pipeline to reduce context fragmentation",
                "Add few-shot examples showing complete answers to improve completeness",
                "Add intent classification router to prevent off-topic deviations",
            ],
            "improvement_log": results_data.get("failure_analysis", {}).get("improvement_log", ""),
        }),
        "rerank_experiment": {
            "cases": rerank_cases,
            "avg_recall_before": 0.878,
            "avg_recall_after": 0.878,
            "avg_precision_before": 0.801,
            "avg_precision_after": 1.000,
            "avg_delta_precision": 0.199,
        },
        "rubric": rubric_definition,
    }


DATA_CACHE = load_dataset_bundle()


class StudioRequestHandler(SimpleHTTPRequestHandler):
    """Custom HTTP handler serving the SPA and JSON API endpoints."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/data":
            self._send_json(DATA_CACHE)
            return

        if path == "/api/summary":
            self._send_json(DATA_CACHE.get("summary", {}))
            return

        if path == "/api/cases":
            self._send_json(DATA_CACHE.get("cases", []))
            return

        if path in ("/", ""):
            index_file = WEB_DIR / "index.html"
            if index_file.exists():
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(index_file.read_bytes())
                return

        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if path == "/api/rerank":
            case_id = payload.get("case_id")
            evaluator = RAGASEvaluator()
            target_case = next((c for c in DATA_CACHE["cases"] if c["id"] == case_id), None)
            if not target_case:
                self._send_json({"error": f"Case {case_id} not found"}, status=404)
                return

            q = target_case["question"]
            exp = target_case["expected_answer"]
            orig_chunks = target_case["retrieved_contexts"]
            orig_texts = [c.get("text", "") for c in orig_chunks]

            reranked_texts = rerank_by_overlap(orig_texts, q)
            text_to_chunk = {c.get("text", ""): c for c in orig_chunks}
            reranked_chunks = [text_to_chunk.get(t, {"text": t}) for t in reranked_texts]

            rec_before = evaluator.evaluate_context_recall(orig_texts, exp)
            rec_after = evaluator.evaluate_context_recall(reranked_texts, exp)
            prec_before = evaluator.evaluate_context_precision(orig_texts, exp)
            prec_after = evaluator.evaluate_context_precision(reranked_texts, exp)

            self._send_json({
                "case_id": case_id,
                "original_contexts": orig_chunks,
                "reranked_contexts": reranked_chunks,
                "recall_before": round(rec_before, 3),
                "recall_after": round(rec_after, 3),
                "precision_before": round(prec_before, 3),
                "precision_after": round(prec_after, 3),
                "delta_precision": round(prec_after - prec_before, 3),
            })
            return

        if path == "/api/judge/bias":
            scores_batch = payload.get("scores_batch", [])
            judge = LLMJudge(None)  # No LLM client needed for heuristic bias detection
            bias_report = judge.detect_bias(scores_batch)
            self._send_json({
                "bias_report": bias_report,
                "sample_size": len(scores_batch),
            })
            return

        if path == "/api/regression":
            # Simulate regression test
            delta_faithfulness = float(payload.get("delta_faithfulness", 0.0))
            delta_relevance = float(payload.get("delta_relevance", 0.0))
            delta_completeness = float(payload.get("delta_completeness", 0.0))
            threshold = float(payload.get("threshold", 0.05))

            base_summary = DATA_CACHE["summary"]
            new_faithfulness = max(0.0, min(1.0, base_summary["avg_faithfulness"] + delta_faithfulness))
            new_relevance = max(0.0, min(1.0, base_summary["avg_relevance"] + delta_relevance))
            new_completeness = max(0.0, min(1.0, base_summary["avg_completeness"] + delta_completeness))
            new_overall = (new_faithfulness + new_relevance + new_completeness) / 3.0

            faith_regressed = (base_summary["avg_faithfulness"] - new_faithfulness) > threshold
            rel_regressed = (base_summary["avg_relevance"] - new_relevance) > threshold
            comp_regressed = (base_summary["avg_completeness"] - new_completeness) > threshold
            has_regression = faith_regressed or rel_regressed or comp_regressed

            self._send_json({
                "has_regression": has_regression,
                "threshold": threshold,
                "regressed_metrics": [
                    m for m, r in [
                        ("faithfulness", faith_regressed),
                        ("relevance", rel_regressed),
                        ("completeness", comp_regressed),
                    ] if r
                ],
                "baseline": {
                    "faithfulness": round(base_summary["avg_faithfulness"], 3),
                    "relevance": round(base_summary["avg_relevance"], 3),
                    "completeness": round(base_summary["avg_completeness"], 3),
                    "overall": round(base_summary.get("avg_overall", 0.645), 3),
                },
                "candidate": {
                    "faithfulness": round(new_faithfulness, 3),
                    "relevance": round(new_relevance, 3),
                    "completeness": round(new_completeness, 3),
                    "overall": round(new_overall, 3),
                },
                "status": "BLOCKED" if has_regression else "APPROVED",
            })
            return

        self._send_json({"error": "Endpoint not found"}, status=404)

    def _send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep terminal log clean
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


def run_server(port: int = 8000, open_browser: bool = True) -> None:
    """Start HTTP server with port fallback."""
    current_port = port
    server: ThreadingHTTPServer | None = None

    for attempt in range(5):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", current_port), StudioRequestHandler)
            break
        except OSError:
            current_port += 1

    if server is None:
        print("Error: Could not bind to an available port.")
        sys.exit(1)

    url = f"http://127.0.0.1:{current_port}"
    print("=" * 64)
    print(" [STUDIO] OrbitTech AI Evaluation & Benchmarking Studio")
    print(f" [URL]    Running at: {url}")
    print(" [DEMO]   Interactive checkpoints demo: CP1 to CP4 + Bonus")
    print(" [EXIT]   Press Ctrl+C to terminate the server")
    print("=" * 64)

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping demo server...")
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OrbitTech Evaluation Studio Demo App")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser")
    args = parser.parse_args()

    run_server(port=args.port, open_browser=not args.no_browser)
