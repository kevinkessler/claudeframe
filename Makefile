PI_HOST ?= pictureframe.lan
PI_USER ?= pi
PI_DEST ?= /home/pi/claudeframe
SSH := ssh $(PI_USER)@$(PI_HOST)
RSYNC := rsync -az --delete --exclude=__pycache__ --exclude='*.pyc' --exclude='.git' --exclude='*.sqlite*' --exclude='*.log'

.PHONY: deploy install-user-unit enable start stop restart status logs tail disable-old-picframe test clean

deploy:
	$(RSYNC) claudeframe/ $(PI_USER)@$(PI_HOST):$(PI_DEST)/claudeframe/
	rsync -az --exclude=__pycache__ --exclude='*.pyc' config/ $(PI_USER)@$(PI_HOST):$(PI_DEST)/config/
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

test:
	python3 -m pytest tests/ -v

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete
