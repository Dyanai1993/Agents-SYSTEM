PY ?= python3
.PHONY: test guard dashboard evals demo timeline
test:      ; $(PY) -m unittest discover -s tests -t . -v
guard:     ; $(PY) -m agentsys.billing_guard
dashboard: ; $(PY) dashboard/server.py 8765
evals:     ; $(PY) evals/run_evals.py selfcheck
demo:      ; $(PY) scripts/seed_demo.py
timeline:  ; $(PY) scripts/timeline.py $(TASK)
