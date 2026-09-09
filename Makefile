.PHONY: test package clean
VERSION ?= 0.4.0
test:
	python3 -m unittest discover -s tests -v
	python3 -m py_compile src/creator_catcher/app.py
package: test
	./packaging/build-deb.sh "$(VERSION)"
clean:
	find dist -maxdepth 1 -type f -name 'creator-catcher_*.deb*' -delete
