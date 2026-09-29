# Analytics

Analytics delivery retains the buffer for a later retry when the delivery call raises an exception. If Kinesis instead reports per-record failures, the failures are logged and the buffer is cleared; those records are not retried.


{% content-ref url="opensearch/" %}
[opensearch](opensearch/)
{% endcontent-ref %}
