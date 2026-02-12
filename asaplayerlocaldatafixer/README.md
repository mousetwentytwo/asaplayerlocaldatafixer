# asaplayerlocaldatafixer — Library

```python
from asaplayerlocaldatafixer.asa import PlayerLocalData

pld = PlayerLocalData('PlayerLocalData.arkprofile')
print(pld)              # <PlayerLocalData … items=50 dinos=2>
data = pld.to_dict()    # or pld.to_json()

# Clear all ARK items
data['data']['MyArkData']['data']['ArkItems'].update(
    {'value': [], 'length': 0, '_size': 4})
PlayerLocalData.from_dict(data).save('modified.arkprofile')
```

> **Tip:** `_`-prefixed fields are internal metadata the writer needs — don't remove them.
