from prometheus_client import Counter, Histogram
# Счётчик количества запросов
# - 'http_requests_total' — имя метрики
# - 'Total HTTP requests' — описание (будет видно в документации)
# - ['method', 'endpoint', 'status'] — метки (labels), по которым можно фильтровать
REQUEST_COUNT = Counter(
'http_requests_total',
'Total HTTP requests',
['method', 'endpoint', 'status']
)
# Гистограмма времени выполнения запросов
# - 'http_request_duration_seconds' — имя метрики
# - 'HTTP request latency' — описание
# - ['method', 'endpoint'] — метки (без status, потому что время ответа не зависит от статуса)
REQUEST_LATENCY = Histogram(
'http_request_duration_seconds',
'HTTP request latency',
['method', 'endpoint']
)