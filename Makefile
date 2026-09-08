# Ruff, pinned to the same version core's Makefile pins, so a file formatted
# here and a file formatted there come out identical.
RUFF = uvx ruff@0.15.20

lint:
	$(RUFF) check .
	$(RUFF) format --check .

format:
	$(RUFF) check --fix .
	$(RUFF) format .

# Runs inside the core dev stack, where Django and the database live.
test:
	cd ../.. && $(MAKE) dev-test TEST_ARGS="georiva_publisher_forti"
