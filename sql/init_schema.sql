-- Esquema de destino en SQL Server para connector-td-engine.
-- Ejecutar una vez, con un usuario con permisos DDL, antes del primer arranque del contenedor.
-- Compatible con cualquier cliente SQL (sin GO).

-- Paso 1: limpiar esquema eic si se creó antes (ejecutar solo si existe)
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_eic_TagsHourly_Window' AND object_id = OBJECT_ID('eic.TagsHourly'))
    EXEC('DROP INDEX IX_eic_TagsHourly_Window ON eic.TagsHourly');

IF EXISTS (SELECT 1 FROM sys.tables WHERE name = 'TagsHourly' AND schema_id = SCHEMA_ID('eic'))
    EXEC('DROP TABLE eic.TagsHourly');

IF EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'eic')
    EXEC('DROP SCHEMA eic');

-- Paso 2: tabla unificada en dbo
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'EicTagsHourly' AND schema_id = SCHEMA_ID('dbo'))
    EXEC('
        CREATE TABLE dbo.EicTagsHourly (
            TagName     VARCHAR(128)  NOT NULL,
            WindowStart DATETIME2(0)  NOT NULL,
            AggType     VARCHAR(4)    NOT NULL CONSTRAINT CK_EicTagsHourly_AggType CHECK (AggType IN (''twa'', ''last'')),
            WindowEnd   AS DATEADD(hour, 1, WindowStart) PERSISTED,
            Value       FLOAT         NULL,
            SyncedAt    DATETIME2(3)  NOT NULL CONSTRAINT DF_EicTagsHourly_SyncedAt DEFAULT SYSUTCDATETIME(),
            CONSTRAINT PK_EicTagsHourly PRIMARY KEY CLUSTERED (TagName, WindowStart, AggType)
        )
    ');

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_EicTagsHourly_Window' AND object_id = OBJECT_ID('dbo.EicTagsHourly'))
    EXEC('
        CREATE NONCLUSTERED INDEX IX_EicTagsHourly_Window
            ON dbo.EicTagsHourly (WindowStart, WindowEnd, AggType) INCLUDE (TagName, Value)
    ');
