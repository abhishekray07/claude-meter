REMOTE_URL := $(shell git remote get-url origin)

dashboard:
	mkdir -p /tmp/claude-meter-dashboard
	python3 analysis/dashboard.py ~/.claude-meter --output /tmp/claude-meter-dashboard/index.html
	cd /tmp/claude-meter-dashboard && \
		git init && \
		git checkout -B gh-pages && \
		git add index.html && \
		git commit -m "Update dashboard $$(date -u +%Y-%m-%dT%H:%M:%SZ)" && \
		(git remote get-url origin >/dev/null 2>&1 || git remote add origin $(REMOTE_URL)) && \
		git push origin gh-pages --force

dashboard-local:
	python3 analysis/dashboard.py ~/.claude-meter --output index.html --open

.PHONY: dashboard dashboard-local
