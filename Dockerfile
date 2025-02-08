FROM pytorch/pytorch:1.13.1-cuda11.6-cudnn8-runtime

COPY ./requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt && \\
    pip uninstall -y opencv-python && \\
    pip install --no-cache-dir roboflow

COPY ./detect.py /app/detect.py
COPY ./evolve.py /app/evolve.py
COPY ./export.py /app/export.py
COPY ./train.py /app/train.py
COPY ./val.py /app/val.py
COPY ./data /app/data
COPY ./models /app/models
COPY ./hyp.yaml /app/hyp.yaml

WORKDIR /app
