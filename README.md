# GREYWARD Office updates

Public distribution component for GREYWARD Writer, Sheets, PDF and Slides.
These customized applications are based on the work of the ArtCraft team:
[WordCraft](https://github.com/storytold/wordcraft), [GridCraft](https://github.com/storytold/gridcraft), [PdfCraft](https://github.com/storytold/pdfcraft), [DeckCraft](https://github.com/storytold/deckcraft).

GREYWARD branding does not make these official ArtCraft builds. Original
licenses, attribution and corresponding sources accompany each generation.

The automation policy starts disabled. Manual pilot runs build and test only.
The channel is not available until signed publication and native DNF readback
have passed. See the workflow results and signed channel state for its actual status.

Once commissioned: complete stable-version check every six days; preserve
the current and two preceding complete four-app generations. An obsolete
fourth generation can remain for 48 hours during safe expiry. Interrupted
staging drafts also expire after 48 hours. No OS releases are managed here.

Builds have no signing or publication credentials. A separate transfer job
moves one application at a time into a private draft and removes the Actions
artifact; signing is a different job. Native DNF verification and a pinned
public key gate the channel. Failed compatibility checks keep the prior version.

The same adaptation code checks candidates and builds RPMs. Changes in the
surrounding lines are accepted when the reviewed edit still matches exactly and
uniquely in the same file. Insertions require their original context. Update
comparison also keeps and verifies new notices for already approved third-party
license terms. Changes to reviewed terms or ambiguous adaptations require review.


This repository is a reviewed component export of [GREYWARD](https://github.com/MANTIS-devlab/GREYWARD), not a development-machine image.
