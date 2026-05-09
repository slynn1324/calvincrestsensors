# LORA CAD detection logs

```
[12:15:00.472] [D][lora_tx:718]: TX: ping|70a524|64

detection loop 1
vvv
[12:15:00.472] [D][sx126x:687]: CAD detection started
[12:15:00.472] [D][sx126x:552]: set_mode_standby RadioState->IDLE
[12:15:00.472] [D][sx126x:556]: setting pa_ctx_pin 5 LOW for STANDBY
[12:15:00.472] [D][sx126x:748]: CAD detect loop
[12:15:00.473] [D][sx126x:748]: CAD detect loop
[12:15:00.473] [D][sx126x:748]: CAD detect loop
[12:15:00.473] [D][sx126x:488]: set_mode_rx RadioState->RX_LISTENING
[12:15:00.473] [D][sx126x:769]: CAD result: busy (IRQ=0x0180, SF9)
^^^
result: CAD determined the air was busy


detection loop 2
vvv
[12:15:00.473] [D][sx126x:687]: CAD detection started
[12:15:00.473] [D][sx126x:699]: CAD skipped: preamble in register (0x0004)
^^^
result: rx in progress, preamble in register


detection loop 3
vvv
[12:15:00.473] [D][sx126x:687]: CAD detection started
[12:15:00.473] [D][sx126x:699]: CAD skipped: preamble in register (0x0004)
^^^
result: rx in progress, preamble in register


detection loop 4
vvv
[12:15:00.473] [D][sx126x:687]: CAD detection started
[12:15:00.473] [D][sx126x:699]: CAD skipped: preamble in register (0x0004)
^^^
result: rx in progress, preamble in register


detection loop 5
vvv
[12:15:00.527] [D][sx126x:687]: CAD detection started
[12:15:00.529] [D][sx126x:699]: CAD skipped: preamble in register (0x0004)
^^^
result: rx in progress, preamble in register


detection loop 6
vvv
[12:15:00.586] [D][sx126x:687]: CAD detection started
[12:15:00.587] [D][sx126x:552]: set_mode_standby RadioState->IDLE
[12:15:00.589] [D][sx126x:556]: setting pa_ctx_pin 5 LOW for STANDBY
[12:15:00.617] [D][sx126x:748]: CAD detect loop
[12:15:00.617] [D][sx126x:748]: CAD detect loop
[12:15:00.617] [D][sx126x:748]: CAD detect loop
[12:15:00.646] [D][sx126x:488]: set_mode_rx RadioState->RX_LISTENING
[12:15:00.648] [D][sx126x:769]: CAD result: clear (IRQ=0x0080, SF9)
^^^
result: rx complete, no more preamble in register, CAD says air is clear


transmit packet
vvv
[12:15:00.648] [D][sx126x:552]: set_mode_standby RadioState->IDLE
[12:15:00.648] [D][sx126x:556]: setting pa_ctx_pin 5 LOW for STANDBY
[12:15:00.648] [D][sx126x:503]: setting pa_ctx_pin 5 HIGH for TX
[12:15:00.651] [D][sx126x:521]: set_mode_tx RadioState->TX_ACTIVE
[12:15:00.797] [D][sx126x:488]: set_mode_rx RadioState->RX_LISTENING
[12:15:00.799] [D][set_status:615]: ping @65 (ttl 5000 ms)
^^^
```