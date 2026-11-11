"""Import all ORM models so Base.metadata is fully populated (e.g. for Alembic)."""
from app.models.system import Role, User, UserRole, Permission, RolePermission, SystemSetting, AuditLog, RefreshToken, LoginFailure  # noqa
from app.models.device import (  # noqa
    DeviceModel, Device, DeviceComponent, Gateway, GatewayDevice, DeviceCalibrationRecord
)
from app.models.measurement import MeasurementImport, Measurement  # noqa
from app.models.alarm import AlarmRule, Alarm  # noqa
from app.models.maintenance import (  # noqa
    WorkOrder, MaintenanceRecord, MaintenanceSOP, SparePart,
    Asset, AssetDocument, LabSample, LabCorrelationResult, LabAICertification
)
from app.models.knowledge import KnowledgeDocument, KnowledgeChunk  # noqa
from app.models.ai import AIDiagnosis, AIDiagnosisFeedback, AIPrediction, AIHealthScore, AIChatSession, AIChatMessage  # noqa
from app.models.analysis import DataAnalysisDataset, DashboardSnapshot, Notification  # noqa
from app.models.external_source import (  # noqa
    ExternalSource, ExternalDatabase, ExternalSyncLog, DataContinuityGap
)

from app.models.template import (  # noqa
    TemplateRegistry, TemplateVersion, TemplateImportLog, TemplateUsageLog, TemplateDependency
)
from app.models.industrial import (  # noqa
    CommunicationConnection, DeviceTag, TagMapping, AcquisitionEvent, DataQualityEvent,
    ImportValidation, AIModelVersion, AIFeedback
)
