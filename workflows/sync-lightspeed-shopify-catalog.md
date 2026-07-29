# Lightspeed naar Shopify catalogussync

## Doel

Shopify betrouwbaar bijwerken vanuit Lightspeed zonder handmatig gecureerde
content te overschrijven. Iedere muterende run begint met een read-only
preflight, gebruikt verse bronbestanden en eindigt met een onafhankelijke
nacontrole.

## Huidige systeemgrens

- C-Series CSV-export is tijdelijk de beschikbare product- en voorraadbron.
- Lightspeed is leidend voor nieuwe producten, variantidentiteit, prijs,
  voorraad, vendor en zichtbaarheid.
- R-Series wordt uiteindelijk de enige leidende voorraadbron.
- C-Series en R-Series mogen nooit tegelijk voorraad naar Shopify schrijven.
- Er zijn nog geen R-Series credentials, API-adapter of getest datacontract in
  deze workspace. De overstap is daarom voorbereid, maar nog niet gebouwd.

## Veldverantwoordelijkheid

| Veld | Bron | Automatisch gedrag |
| --- | --- | --- |
| Lightspeed product-ID | Lightspeed | Altijd vastleggen in `custom.lightspeed_internal_id` |
| Lightspeed variant-ID | Lightspeed | Bij nieuwe varianten vastleggen in `custom.lightspeed_c_series_variant_id` |
| Titel en handle | Lightspeed bij creatie | Bestaande waarden alleen rapporteren; handlewijzigingen vereisen review |
| Vendor | Lightspeed | Bij creatie invullen; bestaande afwijkingen rapporteren voordat collectieregels worden geraakt |
| Brand-metafield | Afgeleid van vendor | Bij creatie invullen en afwijkingen rapporteren |
| Varianten, SKU en barcode | Lightspeed | Bij creatie invullen; lege of dubbele identifiers rapporteren |
| Prijs | Lightspeed | Exact gematchte varianten mogen automatisch worden bijgewerkt |
| Voorraad | Lightspeed | Beschikbare voorraad op `Neighbourhood Store` bijwerken |
| Productstatus | Lightspeed plus kwaliteit | Alleen sync-beheerde producten automatisch `ACTIVE` of `DRAFT` maken |
| Omschrijving | Shopify na creatie | Eenmalig importeren; daarna nooit door de normale sync overschrijven |
| Afbeeldingen | Shopify na creatie | Eenmalig importeren; daarna nooit door de normale sync overschrijven |
| SEO-velden | Shopify | Nooit door de catalogussync overschrijven |
| Archivering/verwijdering | Handmatig besluit | Nooit automatisch door een normale sync |

## Statusbeleid

Een sync-beheerd product mag alleen `ACTIVE` zijn wanneer:

- Lightspeed `Visible` gelijk is aan `Y` of `S`;
- de totale bronvoorraad groter is dan nul;
- er minimaal een productafbeelding is;
- de vereiste product- en variantidentiteit geldig is.

Anders wordt de gewenste status `DRAFT`. `Visible N` betekent dus draft.
Negatieve voorraad wordt als dataprobleem gerapporteerd en voor Shopify naar
nul begrensd.

Alle 1.060 geldige Lightspeed-producten zijn gekoppeld via
`custom.lightspeed_internal_id`. Daarvan worden 977 producten automatisch op
status gereconcilieerd. De 83 oorspronkelijke legacy drafts dragen
`custom.catalog_status_sync=manual_review` en blijven buiten automatische
statuswijzigingen totdat ze afzonderlijk zijn goedgekeurd. Twee operationele
Lightspeed-regels voldoen niet aan de productkwaliteitspoort en worden niet
naar Shopify geïmporteerd.

## Uitgesloten catalogusitems

Normale runs sluiten bewust verwijderde testproducten en deze retired vendors
uit:

- Messyweekend
- POP Trading Company
- encens d'auroville

Gebruik de override-flags alleen na een expliciet productniveau-besluit.

## Vereiste input

- Een verse Lightspeed productexport.
- Een verse Lightspeed voorraadexport uit hetzelfde exportmoment.
- Shopify app credentials in `.env`.
- Shopify scopes:
  `write_products`, `write_inventory`, `read_locations`, `write_content`,
  `write_metaobject_definitions`, `write_metaobjects`.
- Voeg `write_publications` toe voordat publicatie volledig naar de moderne
  GraphQL-publicatie-API wordt overgezet.

Bronbestanden ouder dan 24 uur zijn standaard onveilig voor `--apply`.

## Read-only preflight

```powershell
python tools\run-catalog-sync-audit.py `
  --products-source "C:\pad\naar\products.csv" `
  --inventory-source "C:\pad\naar\inventory.csv"
```

De preflight maakt:

- een gehashte Shopify-baseline;
- een catalogus- en kwaliteitsrapport;
- een productidentiteitsplan;
- een productimportplan;
- een variantstructuurplan;
- een voorraadplan;
- een prijsplan;
- een statusplan;
- een gecombineerd `catalog-sync-preflight.json`.

De preflight moet nul unmatched varianten, nul dubbele matches, nul
identiteitsconflicten en verse bronnen melden.

## Gecontroleerde apply-volgorde

1. Exporteer verse product- en voorraadbestanden.
2. Draai de volledige read-only preflight.
3. Beoordeel nieuwe producten, negatieve voorraad, prijswijzigingen en
   statuswijzigingen.
4. Valideer of herstel eerst de Lightspeed-productidentiteit:

```powershell
python tools\sync-lightspeed-product-identities.py `
  --source "C:\pad\naar\products.csv" `
  --prefer-client-credentials `
  --apply
```

5. Maak nieuwe producten aan als draft:

```powershell
python tools\import-lightspeed-products.py `
  --source "C:\pad\naar\products.csv" `
  --prefer-client-credentials `
  --apply
```

6. Synchroniseer de variantstructuur:

```powershell
python tools\sync-lightspeed-variants.py `
  --products-source "C:\pad\naar\products.csv" `
  --prefer-client-credentials `
  --apply
```

7. Synchroniseer voorraad:

```powershell
python tools\sync-lightspeed-inventory.py `
  --products-source "C:\pad\naar\products.csv" `
  --inventory-source "C:\pad\naar\inventory.csv" `
  --prefer-client-credentials `
  --apply
```

8. Synchroniseer prijzen:

```powershell
python tools\sync-lightspeed-prices.py `
  --products-source "C:\pad\naar\products.csv" `
  --prefer-client-credentials `
  --apply
```

9. Exporteer opnieuw een Shopify-baseline en maak een nieuw catalogusrapport.
10. Pas pas daarna de gewenste productstatussen toe:

```powershell
python tools\sync-lightspeed-product-status.py `
  --audit-report ".tmp\...\catalog-audit.json" `
  --prefer-client-credentials `
  --apply
```

11. Bewaar alle plannen, resultaten en verificatiefouten bij dezelfde run.

## Beveiligingen

- Dry-run is overal de standaard.
- Bronbestanden krijgen een SHA-256 hash en leeftijd.
- Import maakt nieuwe producten standaard als draft aan.
- Bestaande omschrijvingen en afbeeldingen worden niet bijgewerkt.
- Producten met `custom.catalog_status_sync=manual_review` worden nooit door
  de normale statusrun gepubliceerd of verborgen.
- Statusupdates vereisen live Lightspeed-identiteit en een recente audit.
- Voorraadaanpassingen gebruiken compare-and-set en idempotency keys.
- Prijsupdates zijn per exact gematchte productvariant en per product atomair.
- Iedere apply leest Shopify opnieuw en faalt bij verificatieverschillen.
- Normale runs verwijderen of archiveren niets.

## R-Series overstap

1. Leg R-Series product-, variant-, locatie- en voorraad-ID's vast.
2. Bouw een read-only adapter naar hetzelfde genormaliseerde catalogusmodel.
3. Vergelijk C-Series en R-Series minimaal twee volledige voorraadcycli.
4. Los alle identifier- en locatieverschillen op.
5. Stop de C-Series voorraadwriter.
6. Activeer R-Series als enige voorraadwriter.
7. Houd productcreatie in Lightspeed en behoud de Shopify-metafieldidentiteit.
8. Bewaar een rollback waarbij slechts een voorraadwriter tegelijk actief kan
   zijn.

## Open operationele taken

- Volgende gecontroleerde run opnieuw met verse exports uitvoeren.
- Beoordeel de 83 vergrendelde legacy drafts afzonderlijk; verwijder hun
  `manual_review`-slot alleen na een expliciet productbesluit.
- Beoordeel het ene Shopify-only product en de ene bewust bewaarde oude
  Shopify-variant zonder deze automatisch te verwijderen.
- Corrigeer `kleding-herstel-vermaak` en `cbe-fee` in Lightspeed of houd ze
  expliciet buiten de verkoopcatalogus.
- `write_publications` toevoegen en GraphQL-publicatie activeren.
- R-Series API-toegang en locatie-ID's verkrijgen.
- Read-only preflight dagelijks inplannen.
- Waarschuwing afleveren bij stale bronnen, conflicts, drift of mislukte
  verificatie.
