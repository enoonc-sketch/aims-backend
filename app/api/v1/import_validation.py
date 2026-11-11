from fastapi import APIRouter, Depends, File, UploadFile, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.api.deps import get_current_user, require_permission
from app.db.session import get_db
from app.models.device import Device, DeviceComponent
from app.models.industrial import ImportValidation
from app.models.measurement import MeasurementImport
from app.services.upload_security import read_upload
from app.services.table_import import read_table, detect_columns, clean_number, clean_datetime

router=APIRouter(prefix="/import-validation", tags=["Import Validation"])

SPEC={
 "device_code":("device_code","device code","设备编号","设备编码","tag_device"),
 "component_name":("component_name","component","tag","测量组分","组分","TAG"),
 "time":("time","timestamp","datetime","时间","时间戳"),
 "value":("value","measurement","数值","测量值","值"),
 "quality":("quality","quality_code","质量","数据质量"),
 "unit":("unit","units","单位")
}

@router.post("/preview", dependencies=[Depends(require_permission("measurement.import"))])
async def preview(file: UploadFile=File(...), db:Session=Depends(get_db)):
    upload=await read_upload(file)
    raw=upload.raw
    try: df=read_table(raw,upload.filename)
    except ValueError as e: raise HTTPException(400,str(e))
    detected=detect_columns(df.columns,{k:tuple(vv.lower() for vv in v) for k,v in SPEC.items()})
    required={k for k in ("device_code","component_name","time","value") if k not in detected}
    issues=[]
    if required: issues.append({"rule_code":"REQUIRED_COLUMN_MISSING","severity":"error","details":{"fields":sorted(required)}})
    unknown_devices=0; unknown_tags=0; bad_values=0; bad_times=0; range_errors=0
    for _,row in df.head(5000).iterrows():
        dc=str(row[detected["device_code"]]).strip() if "device_code" in detected else ""
        cn=str(row[detected["component_name"]]).strip() if "component_name" in detected else ""
        if dc and db.scalar(select(Device.id).where(Device.device_code==dc)) is None: unknown_devices+=1
        if dc and cn:
            dev=db.scalar(select(Device).where(Device.device_code==dc))
            if dev and db.scalar(select(DeviceComponent.id).where(DeviceComponent.device_id==dev.id,DeviceComponent.component_name==cn)) is None: unknown_tags+=1
        if "value" in detected and clean_number(row[detected["value"]]) is None: bad_values+=1
        if "time" in detected and clean_datetime(row[detected["time"]]) is None: bad_times+=1
    if unknown_devices: issues.append({"rule_code":"UNKNOWN_DEVICE","severity":"error","count":unknown_devices})
    if unknown_tags: issues.append({"rule_code":"UNKNOWN_TAG","severity":"warning","count":unknown_tags})
    if bad_values: issues.append({"rule_code":"VALUE_NOT_NUMERIC","severity":"error","count":bad_values})
    if bad_times: issues.append({"rule_code":"INVALID_TIMESTAMP","severity":"error","count":bad_times})
    status="rejected" if any(i["severity"]=="error" for i in issues) else ("warning" if issues else "passed")
    # 审计项 F-9：校验结果落库（以前 ImportValidation 表只导入未写入，校验记录
    # 无法追溯）。预览本身没有正式导入批次，因此建一条 status="validating"
    # 的批次记录（device_id 留空），校验结果挂在它下面，可长期审计追溯。
    import_row = MeasurementImport(
        file_name=upload.filename,
        row_count=len(df),
        column_count=len(df.columns),
        status="validating",
        source="local",
    )
    db.add(import_row)
    db.flush()
    db.add(ImportValidation(
        import_id=import_row.id,
        status=status,
        summary={"rows": len(df), "columns": len(df.columns)},
        mapping=detected,
        issues=issues,
    ))
    db.commit()
    return {"status":status,"rows":len(df),"columns":len(df.columns),"detected_columns":detected,"issues":issues,
            "import_id":import_row.id,
            "sample":df.head(10).fillna("").to_dict(orient="records")}
