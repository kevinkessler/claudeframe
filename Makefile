PI_HOST ?= 192.168.132.89
PI_USER ?= pi
PI_DEST ?= /home/pi/claudeframe
SSH := ssh $(PI_USER)@$(PI_HOST)
RSYNC := rsync -az --delete --exclude=__pycache__ --exclude='*.pyc' --exclude='.git' --exclude='*.sqlite*' --exclude='*.log'

FRAME2_HOST ?= 192.168.40.99
FRAME2_USER ?= kevin
FRAME2_DEST ?= /home/kevin/claudeframe
FRAME2_SSH := ssh $(FRAME2_USER)@$(FRAME2_HOST)

.PHONY: deploy install-user-unit enable start stop restart status logs tail disable-old-picframe test clean \
	check-frame2-config deploy-frame2 enable-frame2 start-frame2 stop-frame2 restart-frame2 status-frame2 logs-frame2 tail-frame2

deploy:
	$(RSYNC) claudeframe/ $(PI_USER)@$(PI_HOST):$(PI_DEST)/claudeframe/
	rsync -az --exclude=__pycache__ --exclude='*.pyc' --exclude=claudeframe.yaml config/ $(PI_USER)@$(PI_HOST):$(PI_DEST)/config/
	$(RSYNC) systemd/ $(PI_USER)@$(PI_HOST):$(PI_DEST)/systemd/
	$(SSH) 'mkdir -p ~/.config/systemd/user && cp $(PI_DEST)/systemd/claudeframe.service ~/.config/systemd/user/ && systemctl --user daemon-reload'

install-user-unit: deploy
	$(SSH) 'systemctl --user enable claudeframe.service'

enable:
	$(SSH) 'systemctl --user enable claudeframe.service'

start:
	$(SSH) 'systemctl --user start claudeframe.service'

stop:
	$(SSH) 'systemctl --user stop claudeframe.service'

restart:
	$(SSH) 'systemctl --user restart claudeframe.service'

status:
	$(SSH) 'systemctl --user status claudeframe.service --no-pager'

logs:
	$(SSH) 'journalctl --user -u claudeframe.service -n 200 --no-pager'

tail:
	$(SSH) 'journalctl --user -u claudeframe.service -f'

disable-old-picframe:
	$(SSH) 'systemctl --user stop picframe.service && systemctl --user disable picframe.service'

check-frame2-config:
	$(FRAME2_SSH) 'test -r $(FRAME2_DEST)/config/claudeframe.yaml || { echo "ERROR: readable live config missing: $(FRAME2_DEST)/config/claudeframe.yaml" >&2; exit 1; }; grep -Eqi "^[[:space:]]*buttons_enabled:[[:space:]]*(true|yes|on)[[:space:]]*$$" $(FRAME2_DEST)/config/claudeframe.yaml || { echo "ERROR: buttons_enabled must be true in the frame2 live config" >&2; exit 1; }'

deploy-frame2: check-frame2-config
	$(RSYNC) claudeframe/ $(FRAME2_USER)@$(FRAME2_HOST):$(FRAME2_DEST)/claudeframe/
	rsync -az --exclude=__pycache__ --exclude='*.pyc' --exclude=claudeframe.yaml config/ $(FRAME2_USER)@$(FRAME2_HOST):$(FRAME2_DEST)/config/
	$(RSYNC) systemd/ $(FRAME2_USER)@$(FRAME2_HOST):$(FRAME2_DEST)/systemd/
	$(FRAME2_SSH) 'mkdir -p ~/.config/systemd/user ~/.config/claudeframe && chmod 700 ~/.config/claudeframe && cp $(FRAME2_DEST)/systemd/claudeframe-frame2.service ~/.config/systemd/user/claudeframe.service && systemctl --user daemon-reload'

enable-frame2:
	$(FRAME2_SSH) 'systemctl --user enable claudeframe.service'

start-frame2:
	$(FRAME2_SSH) 'systemctl --user start claudeframe.service'

stop-frame2:
	$(FRAME2_SSH) 'systemctl --user stop claudeframe.service'

restart-frame2:
	$(FRAME2_SSH) 'systemctl --user restart claudeframe.service'

status-frame2:
	$(FRAME2_SSH) 'systemctl --user status claudeframe.service --no-pager'

logs-frame2:
	$(FRAME2_SSH) 'journalctl --user -u claudeframe.service -n 200 --no-pager'

tail-frame2:
	$(FRAME2_SSH) 'journalctl --user -u claudeframe.service -f'

test:
	python3 -m pytest tests/ -v

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete
