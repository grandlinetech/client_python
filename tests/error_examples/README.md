

docker run -e PYTHONPATH=/app -v $PWD:/app -p 9101:9101 python:3.6-buster /bin/bash -c "pip install absl-py && python /app/tests/error_examples/slow_response.py"