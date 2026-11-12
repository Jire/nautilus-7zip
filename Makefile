# Convenience targets; install.sh does the real work.
PREFIX ?= /usr/local
DESTDIR ?=
DESTDIR_ARG = $(if $(DESTDIR),--destdir "$(DESTDIR)")

.PHONY: install install-user uninstall uninstall-user test lint pot

install:
	./install.sh --system --prefix "$(PREFIX)" $(DESTDIR_ARG)

install-user:
	./install.sh

uninstall:
	./install.sh --uninstall --system --prefix "$(PREFIX)" $(DESTDIR_ARG)

uninstall-user:
	./install.sh --uninstall

test:
	python3 -m pytest

lint:
	ruff check src extension tests

pot:
	xgettext --from-code=UTF-8 -L Python -k_ -kN_ -kngettext:1,2 -kpgettext:1c,2 \
		--package-name=nautilus-7zip -o po/nautilus-7zip.pot \
		$$(find src extension -name '*.py' | sort)
