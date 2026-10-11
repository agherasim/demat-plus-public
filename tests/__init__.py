# Must stay empty: pytest imports this module before tests/conftest.py, so
# any `demat` import here would run before conftest.py sets DEMAT_HOME to a
# throwaway directory, defeating the test isolation described there.
