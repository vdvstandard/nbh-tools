.PHONY: venv install compile export-classify apply-brands

venv:
	python -m venv .venv

install: venv
	. .venv/bin/activate && python -m pip install --upgrade pip && pip install -r requirements.txt

compile:
	python -m py_compile tools/*.py

export-classify:
	python tools/sync-product-classification.py --export-review

apply-brands:
	python tools/sync-product-brand-metafields.py --apply
