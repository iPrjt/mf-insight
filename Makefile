.PHONY: install test lint demo run check-mail update-navs
install:     ## install dependencies
	pip install -r requirements.txt ruff
test:        ## run the test suite (demo data, no network)
	MF_OFFLINE=1 python -m pytest -q
lint:        ## catch real errors (unused imports, undefined names)
	ruff check --select E9,F63,F7,F82,F401,F841 .
demo:        ## run the app with simulated funds
	MF_OFFLINE=1 uvicorn app.main:app --reload
run:         ## run with live AMFI data (reads .env)
	uvicorn app.main:app --reload --env-file .env
check-mail:  ## import new statements from connected Gmail accounts
	python -m app.jobs check-mail
update-navs: ## store today's AMFI NAVs, refresh held funds, precompute metrics
	python -m app.jobs update-navs
