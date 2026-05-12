# Developer Notes


## YAML lora_pa_ctx control

```yaml
# handled internally by sx126x_cad enhancement
- platform: gpio
    id: lora_pa_ctx # LORA_PA_CTX - set high for transmit and low for listen.
    pin:
      number: 5
      mode:
        output: true
```