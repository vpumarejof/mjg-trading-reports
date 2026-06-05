# MJG TRADING — Proyecto Shopify

## Restricción de acceso
**IMPORTANTE:** Este proyecto SOLO debe ejecutarse con el usuario `valentina@mjgtrading.com`.
Si la sesión pertenece a otra cuenta, no ejecutar ninguna acción.

## Conexión Shopify (automática)
Al iniciar cualquier sesión en este proyecto, conectarse automáticamente con:

- **Store:** business-mjgtrading.myshopify.com
- **API version:** 2025-01
- **Credenciales:** cargadas desde `.env` en esta misma carpeta
- **Token tipo:** offline (permanente, sin expiración)

El token de acceso y las credenciales viven en:
```
/Users/valentinapumarejofabregas/MJG TRADING/.env
```

## Info de la tienda
- **Nombre:** MJG Trading
- **Plan:** Advanced
- **Moneda:** USD
- **Zona horaria:** America/New_York (GMT-5)
- **Ubicación principal:** Teterboro, NJ — location_id: 66181693694
- **Dominio personalizado:** business.mjgtrading.com

## Estructura del proyecto
```
MJG TRADING/
├── .env                  # Credenciales Shopify (NO compartir)
├── oauth_exchange.py     # Script para renovar token si fuera necesario
└── CLAUDE.md             # Este archivo
```
