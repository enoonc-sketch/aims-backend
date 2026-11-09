from datetime import datetime, timezone

def test_industrial_tag_mapping_and_quality(client):
    d = client.post('/api/v1/devices', json={
        'device_code':'Q-001','name':'Quality Device','protocol':'Modbus TCP'
    })
    assert d.status_code in (200,201)
    device_id=d.json()['id']
    g=client.post('/api/v1/gateways', json={'gateway_code':'Q-GW','name':'Q Gateway','protocol':'Modbus TCP'})
    assert g.status_code in (200,201)
    gateway_id=g.json()['id']
    c=client.post('/api/v1/industrial/connections', json={'gateway_id':gateway_id,'name':'Q Conn','protocol':'Modbus TCP','endpoint':'127.0.0.1','port':502})
    assert c.status_code==200
    conn_id=c.json()['id']
    t=client.post('/api/v1/industrial/tags', json={'device_id':device_id,'tag_code':'O2','name':'O2','unit':'%','min_value':0,'max_value':25})
    assert t.status_code==200
    tag_id=t.json()['id']
    m=client.post('/api/v1/industrial/tag-mappings', json={'tag_id':tag_id,'connection_id':conn_id,'address':'40001','scale':0.01})
    assert m.status_code==200
    e=client.post('/api/v1/industrial/acquisition-events', json={'tag_id':tag_id,'connection_id':conn_id,'engineering_value':30,'raw_value':'3000','quality_code':'good','event_time':datetime.now(timezone.utc).isoformat()})
    assert e.status_code==200
    assert e.json()['engineering_value'] in (30,30.0)

def test_import_validation_requires_mapping(client):
    r=client.post('/api/v1/import-validation/preview', files={'file':('x.csv',b'device_code,component_name,time,value\nNOPE,O2,bad,abc\n','text/csv')})
    assert r.status_code==200
    data=r.json()
    assert data['status']=='rejected'
    codes={x['rule_code'] for x in data['issues']}
    assert 'UNKNOWN_DEVICE' in codes
    assert 'VALUE_NOT_NUMERIC' in codes
    assert 'INVALID_TIMESTAMP' in codes
