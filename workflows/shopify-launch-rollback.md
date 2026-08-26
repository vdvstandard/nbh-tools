# Shopify launch rollback

## Doel

Herstel de verkoopbaarheid van Neighbourhood snel en gecontroleerd wanneer de
Shopify-launch checkout, voorraad, catalogus of storefront beschadigt. Behoud
altijd de incidentdata; een rollback is geen reden om rapporten of logs te
verwijderen.

## Verantwoordelijkheid

- Recovery owner: **David**.
- Businessbesluit over prijzen, verzendtarieven, orders en voorraad: de
  Neighbourhood store owner/admin.
- Een vervangende recovery owner moet voor launch nog expliciet worden
  toegewezen als David niet beschikbaar is.

De recovery owner beslist binnen 15 minuten na een bevestigd P0-incident of
de storefront wordt teruggezet. Niemand voert tijdens een rollback catalogus-
of theme-writes buiten deze coordinatie uit.

## Bevestigde herstelpunten

Laatst gecontroleerd op 2026-08-26 met `shopify theme list`, direct na de
Phase 9 theme-publicatie:

- Store: `neighbourhood-arnhem.myshopify.com`
- Huidige live theme: `Launch candidate 8a95917 2026-08-26`
- Live theme ID: `188754329928`
- Development theme ID: `188639904072`
- Theme repository: `C:\Users\david\Documents\neighbourhood-theme`
- Branch: `codex-lookbook-viewer-updates`
- Phase 0 baseline commit: `c55cb3c`
- Laatste gecommitteerde theme-HEAD bij deze controle: `9c4eaa9`
- Actuele pre-launch catalogusbaseline:
  `.tmp/phase9-shopify-baseline-20260811` (verouderd — ververs voor de
  volgende live wijziging)

Beschikbare hersel-/referentiethema's in de Shopify theme library, allemaal
`[unpublished]`:

- `188735193416` — `Rollback before navigation update 2026-08-25`: expliciet
  gemaakte rollback-snapshot van vlak vóór de navigatiewijziging van
  2026-08-25.
- `186898579784` — `Codex lookbook updates 2026-06-30`: het vorige live
  theme, vervangen door de publicatie van `188754329928` op 2026-08-26. Dit
  theme wijkt inhoudelijk af van de huidige repository (bekend van de Phase 9
  vergelijking op 2026-08-11/23) en is dus geen bron voor nieuwe wijzigingen,
  maar wel een bekend werkend storefront-herstelpunt van vóór de launch.

Er zijn dus twee kandidaat-hersteltheme's naast de huidige live theme. Welke
van de twee de juiste rollback-keuze is hangt af van wat er misgaat: gebruik
`188735193416` als het probleem in de navigatiewijziging van 2026-08-25 of
later zit, en `186898579784` als je helemaal terug wilt naar de pre-launch
situatie. Bevestig bij een echt incident eerst met de recovery owner welke van
de twee van toepassing is voordat je publiceert.

Verwijder of overschrijf geen van beide hersteltheme's zolang dit de
bekende herstelpunten zijn.

## Rollbacktriggers

Start een P0-rollback bij een van deze situaties:

- checkout is niet bereikbaar of levert geen verzendmethode voor een bedoelde
  verkoopregio;
- betalingen of orderbevestigingen falen voor meerdere klanten;
- voorraad wordt aantoonbaar dubbel, negatief of vanuit twee systemen
  overschreven;
- productprijzen of publicatiestatussen zijn op grote schaal fout;
- de storefront toont een brede Liquid/JavaScript-fout of onbruikbare cart;
- een privacy- of analyticsconfiguratie verwerkt marketingdata zonder geldige
  toestemming.

Gebruik voor een klein, geisoleerd contentprobleem eerst een gerichte fix. Zet
niet de hele storefront terug voor een enkel verkeerd label of beeld.

## Eerste respons

1. Noteer starttijd, melder, impact, eerste fout en recovery owner.
2. Stop alle handmatige `--apply` catalogusruns, theme pushes en publicaties.
3. Laat C-Series en R-Series nooit tegelijk voorraad schrijven. Er is nu geen
   geautomatiseerde continue writer; start tijdens het incident geen sync.
4. Exporteer of bewaar de actuele foutpagina, order-ID's, auditrapporten en
   screenshots in een nieuwe timestamped `.tmp/incident-*` map.
5. Controleer of nieuwe orders binnenkomen. Annuleer of refund niets
   automatisch.

## Storefront terugzetten

1. Bevestig eerst dat het bekende hersteltheme nog aanwezig is:

```powershell
shopify.cmd theme list --store neighbourhood-arnhem.myshopify.com
```

2. Publiceer het gekozen hersteltheme (zie boven welke van de twee van
   toepassing is; vul het juiste ID in):

```powershell
shopify.cmd theme publish `
  --store neighbourhood-arnhem.myshopify.com `
  --theme <188735193416-of-186898579784>
```

3. Controleer opnieuw met `shopify theme list` welk theme de rol `[live]`
   heeft. Stop wanneer het gekozen ID niet beschikbaar is; publiceer nooit
   op basis van alleen een gelijkende naam.
4. Test homepage, een product, add-to-cart, quantity, remove, pickup en een
   shippingadres. Bevestig minimaal Nederland en Duitsland via checkout met
   zowel een normale cart als een cart boven EUR 300. De Phase 9 preflight vond
   alleen in die duurdere test een ontbrekend NL/DE-tarief; dit is op
   2026-08-26 verholpen in de `Algemeen profiel` delivery profile en herbevestigd
   met een live read-only cart-audit (zie plan Phase 9). Deze stap blijft
   staan om de fix te bevestigen tijdens een echte checkout-poging.
5. Bewaar het defecte launch-theme als unpublished incidentbewijs. Verwijder
   het niet voordat de oorzaak is vastgesteld.

## Catalogus en voorraad herstellen

Een oude voorraadbaseline mag nooit blind naar Shopify worden teruggeschreven.
Voorraad kan sinds het baseline-moment door echte verkopen zijn veranderd.

1. Exporteer eerst de huidige Shopify-state en verse Lightspeed product- en
   voorraadbestanden uit hetzelfde exportmoment.
2. Vergelijk de incidentrun met zijn preflight-, apply- en verify-rapporten.
3. Herstel voorraad en prijzen uitsluitend via de normale workflow in
   `workflows/sync-lightspeed-shopify-catalog.md`, eerst als dry-run.
4. Herstel status- of identitywijzigingen alleen voor de exact geraakte
   product-ID's. Houd `custom.catalog_status_sync=manual_review` intact.
5. Verwijder, archiveer of bulk-publiceer geen producten als onderdeel van een
   noodrollback.
6. Draai na iedere apply een nieuwe onafhankelijke catalogusaudit. Hervat pas
   wanneer die nul onverwachte drift rapporteert.

## Orders, meldingen en klanten

1. Maak in Shopify Admin een lijst van orders vanaf de incidentstart.
2. Controleer per order betaling, gekozen shipping/pickup, voorraadreservering
   en verzonden meldingen.
3. Voer cancellation en refund alleen per order uit na een businessbesluit.
4. Leg vast welke klanten handmatig bericht nodig hebben en welke template is
   gebruikt.

## Heropeningspoort

De recovery owner heropent pas wanneer:

- de live theme-ID expliciet is bevestigd;
- NL en DE elk minimaal een geldige verzendmethode tonen;
- pickup op `Neighbourhood Store` beschikbaar is;
- cart en checkout op desktop en mobiel slagen;
- taxes en prijzen inclusief btw kloppen;
- voorraad met een verse Lightspeed-export is gereconcilieerd;
- notification previews of testorders zijn gecontroleerd;
- consent en de bedoelde conversie-events zonder duplicates zijn bevestigd.

Leg eindtijd, oorzaak, herstelstappen, resterende acties en eigenaar vast in
het incidentrapport. Werk deze workflow bij wanneer de uitvoering een nieuw
randgeval of een betere herstelroute oplevert.
