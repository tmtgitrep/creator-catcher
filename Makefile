.PHONY: test package clean
VERSION ?= 0.1.0
test:
	python3 -m unittest discover -s tests -v
	python3 -m py_compile src/creator_catcher/app.py
package: test
	docker build -t creator-catcher-deb-builder -f packaging/Dockerfile .
	docker run --rm -v "$(CURDIR):/project" creator-catcher-deb-builder "$(VERSION)"
clean:
	find dist -maxdepth 1 -type f -name 'creator-catcher_*.deb*' -delete
