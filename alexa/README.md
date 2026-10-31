# Skill de Alexa (Orbitron por voz)

`lambda_function.py` es el código de la Lambda de AWS (región us-east-2) que usa la skill «Orbyte».
Este archivo es la copia de respaldo: lo que corre de verdad es lo que está desplegado en AWS.

## Actualizar la Lambda

1. AWS → **Lambda** → la función → pestaña **Code** → `lambda_function.py`.
2. Reemplazar todo el contenido por el de este archivo y tocar **Deploy**.

## Configuración (en la Lambda, nunca en este repo)

- `ORBYTE_BASE_URL`: URL pública del Funnel, sin barra final.
- `ORBYTE_TOKEN`: clave de Alexa (`alx_…`), generada en el servidor con `./clave_alexa.sh generar`.
- `DYNAMODB_TABLE_NAME` (opcional): por defecto `OrbyteAlexaSkillState`.

## Diagnóstico

- En el servidor: `./clave_alexa.sh estado` muestra cuándo usó Alexa la clave por última vez.
- En CloudWatch, cada pedido deja `ai/playlist OK en N ms` o el motivo de la falla (conexión, plazo agotado, clave).
