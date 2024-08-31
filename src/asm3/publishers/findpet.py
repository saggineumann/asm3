
import asm3.animal
import asm3.configuration
import asm3.i18n
import asm3.utils

from .base import AbstractPublisher, calc_microchip_data_addresses, get_microchip_data
from asm3.sitedefs import FINDPET_BASE_URL, FINDPET_API_KEY
from asm3.typehints import Database, PublishCriteria, ResultRow

import sys

# ID type keys used in the ExtraIDs column
IDTYPE_FINDPET = "findpet"

class FindPetPublisher(AbstractPublisher):
    """
    Handles sending found pets and microchip registrations to findpet.com
    """
    def __init__(self, dbo: Database, publishCriteria: PublishCriteria) -> None:
        publishCriteria.uploadDirectly = True
        AbstractPublisher.__init__(self, dbo, publishCriteria)
        self.initLog("findpet", "FindPet Publisher")

    def fpDate(self, d):
        """ Returns a date in the format FindPet want """
        if d is None: return ""
        return asm3.i18n.format_date(d)

    def fpLonLat(self, an):
        """ Returns a FindPet [ Lon, Lat ] value """
        latlon = asm3.utils.nulltostr(an.CURRENTOWNERLATLONG).split(",")
        lat = 0
        lon = 0
        if len(latlon) > 1: 
            lat = asm3.utils.cint(latlon[0])
            lon = asm3.utils.cint(latlon[1])
        return [ lon, lat ]
    
    def fpE164(self, pn):
        """ Makes sure a US phone number is formatted to E164 (000-0000) - this should possibly live in base.py """
        # TODO:
        return pn

    def run(self) -> None:
        
        self.log("FindPetPublisher starting...")

        if self.isPublisherExecuting(): return
        self.updatePublisherProgress(0)
        self.setLastError("")
        self.setStartPublishing()

        orgid = asm3.configuration.findpet_org_id(self.dbo)

        if orgid == "":
            self.setLastError("No FindPet Organization ID has been set.")
            self.cleanup()
            return
        
        # Go through our list of shelter animals first, send any shelter animals
        # that have a microchip number, but don't already have a FindPet report_id
        animals = asm3.animal.get_shelter_animals(self.dbo)
        animals = calc_microchip_data_addresses(self.dbo, animals) # We do this so that we have the shelter address for reports

        for an in animals:

            findpet_report_id = asm3.animal.get_extra_id(self.dbo, an, IDTYPE_FINDPET)
            if findpet_report_id != "": 
                self.log(f"Skipping {an.SHELTERCODE} {an.ANIMALNAME} - already reported to FindPet")
                continue

            if an.IDENTICHIPNUMBER == "": 
                self.log(f"Skipping {an.SHELTERCODE} {an.ANIMALNAME} - No microchip number")
                continue

            if not self.validate(an): 
                self.log(f"Skipping {an.SHELTERCODE} {an.ANIMALNAME} - Failed validation")
                continue

            # initiate /report with the shelter animal
            try:
                jsondata = asm3.utils.json(self.processReport(an, orgid))
                url = f"{FINDPET_BASE_URL}/api/report"
                self.log(f"Reporting {an.SHELTERCODE} {an.ANIMALNAME} to FindPet as \"in_shelter\"")
                self.log("Sending POST to %s to create listing: %s" % (url, jsondata))

                r = asm3.utils.post_json(url, jsondata)
                self.log("HTTP %d, headers: %s, response: %s" % (r["status"], r["headers"], r["response"]))

                # store the report_id
                j = asm3.utils.json_parse(r["response"])
                asm3.animal.set_extra_id(self.dbo, "system", an, IDTYPE_FINDPET, j["result"])
            except Exception as err:
                self.logError("Failed sending /report for animal: %s, %s" % (str(an["SHELTERCODE"]), err), sys.exc_info())

        # Now, we can go through the list of microchips that need to be registered
        # and transfer any that have a findpet report_id
        animals = get_microchip_data(self.dbo, ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"], "findpet", allowintake=False)
        if len(animals) == 0:
            self.setLastError("No microchips found to register.")
            return

        processed_animals = []
        failed_animals = []

        anCount = 0
        for an in animals:
            try:
                anCount += 1
                self.log("Processing: %s: %s (%d of %d)" % ( an["SHELTERCODE"], an["ANIMALNAME"], anCount, len(animals)))
                self.updatePublisherProgress(self.getProgress(anCount, len(animals)))

                # If the user cancelled, stop now
                if self.shouldStopPublishing(): 
                    self.log("User cancelled publish. Stopping.")
                    self.resetPublisherProgress()
                    self.cleanup()
                    return

                # We need a FindPet report_id
                findpet_report_id = asm3.animal.get_extra_id(self.dbo, an, IDTYPE_FINDPET)

                if not self.validate(an): continue
                data = self.processTransfer(an)
                jsondata = asm3.utils.json(data)

                # Send the transfer
                url = f"{FINDPET_BASE_URL}/api/pet-transfer"
                self.log("Sending POST to %s to create transfer: %s" % (url, jsondata))
                r = asm3.utils.post_json(url, jsondata)
                j = asm3.utils.json(r["response"])

                # Success is returned as { "result": { "status": "Passed", details: {transfer_id} } }
                # Error codes are returned as { "reason": "message", "details": {transfer_id} }
                if "result" not in j:
                    self.logError("HTTP %d, headers: %s, response: %s" % (r["status"], r["headers"], r["response"]))
                    an.FAILMESSAGE = j["reason"]
                    failed_animals.append(an)
                else:
                    self.log("HTTP %d, headers: %s, response: %s" % (r["status"], r["headers"], r["response"]))
                    self.logSuccess("Processed: %s: %s (%d of %d)" % ( an["SHELTERCODE"], an["ANIMALNAME"], anCount, len(animals)))
                    processed_animals.append(an)

            except Exception as err:
                self.logError("Failed processing animal: %s, %s" % (str(an["SHELTERCODE"]), err), sys.exc_info())

        # Mark sent animals published
        if len(processed_animals) > 0:
            self.markAnimalsPublished(processed_animals)
        if len(failed_animals) > 0:
            self.markAnimalsPublishFailed(failed_animals)

        self.cleanup()

    def processReport(self, an: ResultRow, orgid: str) -> str:
        """ Processes an animal record and returns a data dictionary for upload to /api/report as JSON """
        return {
            "token": FINDPET_API_KEY,
            "report_type": "in_shelter", # seems to be the value they want irrespective of whether it's nonshelter or not
            "photos": [ self.getPhotoUrl(an.ID) ],
            "videos": [],  # not used
            "adt_markings": [], # not used 
            "breeds": [ an.BREEDNAME1, an.BREEDNAME2 ], 
            "colors": [ an.BASECOLOURNAME ],
            "address": {
                "full": f"{an.CURRENTOWNERADDRESS}, {an.CURRENTOWNERTOWN}, {an.CURRENTOWNERCOUNTY}, US",
                "addressline_1": an.CURRENTOWNERNAME,
                "addressline_2": "",
                "city": an.CURRENTOWNERTOWN,
                "county": "",
                "state": an.CURRENTOWNERCOUNTY,
                "country": "US",
                "postal_code": an.CURRENTOWNERPOSTCODE
            },
            "location": {
                "type": "Point",
                "coordinates": self.fpLonLat(an),
            },
            "org_id": orgid, 
            "pet_name": an.ANIMALNAME,
            "pet_org_id": an.SHELTERCODE,
            "pet_type": an.SPECIESNAME,
            "contact_name": an.CURRENTOWNERNAME, 
            "email": an.CURRENTOWNEREMAILADDRESS,
            "phone": self.fpE164(an.CURRENTOWNERMOBILETELEPHONE), # e164 format
            "sms_alerts": self.fpE164(an.CURRENTOWNERMOBILETELEPHONE), # strictly e164 format
            "microchip_id": an.IDENTICHIPNUMBER,
            "event_date": self.fpDate(an.MOSTRECENTENTRYDATE),
            "date_of_birth": self.fpDate(an.DATEOFBIRTH),
            "size": an.SIZENAME,
            "gender": an.SEXNAME,
            "coat_type": an.COATTYPENAME,
            # "coat_length": "Short", # we don't have a coat length field to map so omit it
            "description": self.getDescription(an, crToBr=True),
            "show_phone": 0
        }
    
    def processTransfer(self, an: ResultRow) -> str:
        """ Processes a transfer """
        return {
            "token": FINDPET_API_KEY,
            "address": {   
                "full": f"{an.CURRENTOWNERADDRESS}, {an.CURRENTOWNERTOWN}, {an.CURRENTOWNERCOUNTY}, US",
                "addressline_1": an.CURRENTOWNERNAME,
                "addressline_2": "",
                "city": an.CURRENTOWNERTOWN,
                "county": "",
                "state": an.CURRENTOWNERCOUNTY,
                "country": "US",
                "postal_code": an.CURRENTOWNERPOSTCODE
            },
            "location": {
                "type": "Point",
                "coordinates": self.fpLonLat(an),
            },
            "report_id": asm3.animal.get_extra_id(self.dbo, an, IDTYPE_FINDPET),
            "name": an.CURRENTOWNERNAME, 
            "family_name": an.CURRENTOWNERSURNAME,
            "email": an.CURRENTOWNEREMAILADDRESS,
            "phone": self.fpE164(an.CURRENTOWNERMOBILETELEPHONE) 
        }

    def validate(self, an: ResultRow) -> bool:
        """ Validates a report or transfer record is ok to send """
        # Validate certain items aren't blank so we aren't registering bogus data
        if asm3.utils.nulltostr(an.CURRENTOWNERADDRESS).strip() == "":
            self.logError("Address for the new owner is blank, cannot process")
            return False 
        if asm3.utils.nulltostr(an.CURRENTOWNERPOSTCODE).strip() == "":
            self.logError("Postal code for the new owner is blank, cannot process")
            return False
        # Make sure the length is actually suitable
        if not len(an.IDENTICHIPNUMBER) in (9, 10, 15):
            self.logError("Microchip length is not 9, 10 or 15, cannot process")
            return False
        return True
