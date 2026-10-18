#!/bin/bash
# Ticket 42b: clave propia (que no vence) para la skill de Alexa.
#   ./clave_alexa.sh generar   -> muestra la clave nueva para ORBYTE_TOKEN en AWS
#   ./clave_alexa.sh revocar   -> la anula
#   ./clave_alexa.sh estado    -> si hay una activa y cuándo la usó Alexa
cd "$(dirname "$0")"
exec python3 integraciones.py "$@"
