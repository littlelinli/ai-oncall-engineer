# Lambda kezekshi-llm: языковая модель (llama.cpp). Собирает и выкладывает terraform/llm.tf.
# Модель в образ не входит: она лежит в S3 и копируется в /tmp при холодном старте.

# 1. Сборка llama.cpp (нужны компиляторы, в итоговый образ они не попадают)
FROM public.ecr.aws/lambda/python:3.12 AS build
RUN (dnf install -y gcc-c++ cmake make || microdnf install -y gcc-c++ cmake make) && (dnf clean all || microdnf clean all || true)
COPY requirements.txt /tmp/requirements.txt
# без привязки к процессору сборщика и без OpenMP: в Lambda другой процессор и нет libgomp
ENV CMAKE_ARGS="-DGGML_NATIVE=OFF -DGGML_OPENMP=OFF -DGGML_BLAS=OFF"
RUN pip install --no-cache-dir --target /opt/pkgs -r /tmp/requirements.txt

# 2. Итоговый образ
FROM public.ecr.aws/lambda/python:3.12
COPY --from=build /opt/pkgs ${LAMBDA_TASK_ROOT}/
COPY handler.py ${LAMBDA_TASK_ROOT}/
CMD ["handler.handler"]
