"""   The Job Scheduling Executor takes the information gained from all previous
      optimizers and makes a scheduling decision for the jobs.

      Subsequent to this jobs are added into a Task Queue and pilot agents can be submitted.

      All issues preventing the successful resolution of a site candidate are discovered
      here where all information is available.

      This Executor will fail affected jobs meaningfully.

"""
__RCSID__ = "$Id: $"

import random

from DIRAC import S_OK, S_ERROR

from DIRAC.Core.Utilities.SiteSEMapping                             import getSEsForSite
from DIRAC.Core.Utilities.Time                                      import fromString, toEpoch
from DIRAC.Core.Security                                            import Properties
from DIRAC.ConfigurationSystem.Client.Helpers.Resources             import getSiteTier
from DIRAC.ConfigurationSystem.Client.Helpers                       import Registry
from DIRAC.ConfigurationSystem.Client.Helpers.Operations            import Operations
from DIRAC.StorageManagementSystem.Client.StorageManagerClient      import StorageManagerClient, getFilesToStage
from DIRAC.Resources.Storage.StorageElement                         import StorageElement
from DIRAC.WorkloadManagementSystem.Executor.Base.OptimizerExecutor import OptimizerExecutor
from DIRAC.WorkloadManagementSystem.DB.JobDB                        import JobDB



class JobScheduling( OptimizerExecutor ):
  """
      The specific Optimizer must provide the following methods:
      - optimizeJob() - the main method called for each job
      and it can provide:
      - initializeOptimizer() before each execution cycle
  """

  @classmethod
  def initializeOptimizer( cls ):
    """ Initialization of the optimizer.
    """
    cls.__jobDB = JobDB()
    return S_OK()

  def optimizeJob( self, jid, jobState ):
    """ 1. Banned sites are removed from the destination list.
        2. Get input files
        3. Production jobs are sent directly to TQ
        4. Check if staging is necessary
    """
    # Reschedule delay
    result = jobState.getAttributes( [ 'RescheduleCounter', 'RescheduleTime', 'ApplicationStatus' ] )
    if not result[ 'OK' ]:
      return result
    attDict = result[ 'Value' ]
    try:
      reschedules = int( attDict[ 'RescheduleCounter' ] )
    except ( ValueError, KeyError ):
      return S_ERROR( "RescheduleCounter has to be an integer" )
    if reschedules != 0:
      delays = self.ex_getOption( 'RescheduleDelays', [60, 180, 300, 600] )
      delay = delays[ min( reschedules, len( delays ) - 1 ) ]
      waited = toEpoch() - toEpoch( fromString( attDict[ 'RescheduleTime' ] ) )
      if waited < delay:
        return self.__holdJob( jobState, 'On Hold: after rescheduling %s' % reschedules, delay )

    # Get site requirements
    result = self.__getSitesRequired( jobState )
    if not result[ 'OK' ]:
      return result
    userSites, userBannedSites = result[ 'Value' ]

    # Get job type
    result = jobState.getAttribute( "JobType" )
    if not result[ 'OK' ]:
      return S_ERROR( "Could not retrieve job type" )
    jobType = result[ 'Value' ]

    # Get banned sites from DIRAC
    result = self.__jobDB.getSiteMask( 'Banned' )
    if not result[ 'OK' ]:
      return S_ERROR( "Cannot retrieve banned sites from JobDB" )
    wmsBannedSites = result[ 'Value' ]

    # If the user has selected any site, filter them and hold the job if not able to run
    if userSites:
      if jobType not in self.ex_getOption( 'ExcludedOnHoldJobTypes', [] ):
        sites = self._applySiteFilter( userSites, banned = wmsBannedSites )
        if not sites:
          if len( userSites ) > 1:
            return self.__holdJob( jobState, "Requested sites %s are inactive" % ",".join( userSites ) )
          else:
            return self.__holdJob( jobState, "Requested site %s is inactive" % userSites[0] )

    # Check if there is input data
    result = jobState.getInputData()
    if not result['OK']:
      self.jobLog.error( "Cannot get input data %s" % ( result['Message'] ) )
      return S_ERROR( "Failed to get input data from JobDB" )

    if not result['Value']:
      # No input data? Just send to TQ
      return self.__sendToTQ( jobState, userSites, userBannedSites )

    self.jobLog.verbose( "Has an input data requirement" )
    inputData = result[ 'Value' ]

    # Production jobs are sent to TQ, but first we have to verify if staging is necessary
    if jobType in Operations().getValue( 'Transformations/DataProcessing', [] ):
      self.jobLog.info( "Production job: sending to TQ, but first checking if staging is requested" )

      userName = jobState.getAttribute( 'Owner' )
      if not userName[ 'OK' ]:
        return userName
      userName = userName['Value']

      userGroup = jobState.getAttribute( 'OwnerGroup' )
      if not userGroup[ 'OK' ]:
        return userGroup
      userGroup = userGroup['Value']

      res = getFilesToStage( inputData, proxyUserName = userName, proxyUserGroup = userGroup ) #pylint: disable=unexpected-keyword-arg

      if not res['OK']:
        return self.__holdJob( jobState, res['Message'] )
      stageLFNs = res['Value']['offlineLFNs']
      if stageLFNs:
        res = self.__checkStageAllowed( jobState )
        if not res['OK']:
          return res
        if not res['Value']:
          return S_ERROR( "Stage not allowed" )
        self.__requestStaging( jobState, stageLFNs )
        return S_OK()
      else:
        return self.__sendToTQ( jobState, userSites, userBannedSites )

    # From now on we know it's a user job with input data

    idAgent = self.ex_getOption( 'InputDataAgent', 'InputData' )
    result = self.retrieveOptimizerParam( idAgent )
    if not result['OK']:
      self.jobLog.error( "Could not retrieve input data info", result[ 'Message' ] )
      return S_ERROR( "Could not retrieve input data info" )
    opData = result[ 'Value' ]

    if 'SiteCandidates' not in opData:
      return S_ERROR( "No possible site candidates" )

    # Filter input data sites with user requirement
    siteCandidates = list( opData[ 'SiteCandidates' ] )
    self.jobLog.info( "Site candidates are %s" % siteCandidates )

    siteCandidates = self._applySiteFilter( list( set( siteCandidates ) & set( userSites ) ), banned = userBannedSites )
    if not siteCandidates:
      return S_ERROR( "Impossible InputData * Site requirements" )

    idSites = {}
    for site in siteCandidates:
      idSites[ site ] = opData[ 'SiteCandidates' ][ site ]

    # Check if sites have correct count of disk+tape replicas
    numData = len( inputData )
    errorSites = set()
    for site in idSites:
      if numData != idSites[ site ][ 'disk' ] + idSites[ site ][ 'tape' ]:
        self.jobLog.error( "Site candidate %s does not have all the input data" % site )
        errorSites.add( site )
    for site in errorSites:
      idSites.pop( site )
    if not idSites:
      return S_ERROR( "Site candidates do not have all the input data" )

    # Check if staging is required
    stageRequired, siteCandidates = self.__resolveStaging( jobState, inputData, idSites )
    if not siteCandidates:
      return S_ERROR( "No destination sites available" )

    # Is any site active?
    stageSites = self._applySiteFilter( siteCandidates, banned = wmsBannedSites )
    if not stageSites:
      return self.__holdJob( jobState, "Sites %s are inactive or banned" % ", ".join( siteCandidates ) )

    # If no staging is required send to TQ
    if not stageRequired:
      # Use siteCandidates and not stageSites because active and banned sites
      # will be taken into account on matching time
      return self.__sendToTQ( jobState, siteCandidates, userBannedSites )

    # Check if the user is allowed to stage
    if self.ex_getOption( "RestrictDataStage", False ):
      res = self.__checkStageAllowed( jobState )
      if not res['OK']:
        return res
      if not res['Value']:
        return S_ERROR( "Stage not allowed" )

    # Get stageSites[0] because it has already been randomized and it's as good as any in stageSites
    stageSite = stageSites[0]
    self.jobLog.verbose( " Staging site will be %s" % ( stageSite ) )
    stageData = idSites[ stageSite ]
    # Set as if everything has already been staged
    stageData[ 'disk' ] += stageData[ 'tape' ]
    stageData[ 'tape' ] = 0
    # Set the site info back to the original dict to save afterwards
    opData[ 'SiteCandidates' ][ stageSite ] = stageData

    stageRequest = self.__preRequestStaging( jobState, stageSite, opData )
    if not stageRequest['OK']:
      return stageRequest
    stageLFNs = stageRequest['Value']
    result = self.__requestStaging( jobState, stageLFNs )
    if not result[ 'OK' ]:
      return result
    stageLFNs = result[ 'Value' ]
    self.__updateSharedSESites( jobState, stageSite, stageLFNs, opData )
    # Save the optimizer data again
    self.jobLog.verbose( 'Updating %s Optimizer Info:' % ( idAgent ), opData )
    result = self.storeOptimizerParam( idAgent, opData )
    if not result[ 'OK' ]:
      return result

    return self.__setJobSite( jobState, stageSites )

  def _applySiteFilter( self, sites, banned = False ):
    """ Filters out banned sites
    """
    if not sites:
      return sites

    filtered = set( sites )
    if banned and isinstance( banned, ( list, set, dict ) ):
      filtered -= set( banned )
    return list( filtered )

  def __holdJob( self, jobState, holdMsg, delay = 0 ):
    if delay:
      self.freezeTask( delay )
    else:
      self.freezeTask( self.ex_getOption( "HoldTime", 300 ) )
    self.jobLog.info( "On hold -> %s" % holdMsg )
    return jobState.setAppStatus( holdMsg, source = self.ex_optimizerName() )

  def __getSitesRequired( self, jobState ):
    """Returns any candidate sites specified by the job or sites that have been
       banned and could affect the scheduling decision.
    """

    result = jobState.getManifest()
    if not result[ 'OK' ]:
      return S_ERROR( "Could not retrieve manifest: %s" % result[ 'Message' ] )
    manifest = result[ 'Value' ]

    bannedSites = manifest.getOption( "BannedSites", [] )
    if not bannedSites:
      bannedSites = manifest.getOption( "BannedSite", [] )
    if bannedSites:
      self.jobLog.info( "Banned %s sites" % ", ".join( bannedSites ) )

    sites = manifest.getOption( "Site", [] )
    # TODO: Only accept known sites after removing crap like ANY set in the original manifest
    sites = [ site for site in sites if site.strip().lower() not in ( "any", "" ) ]

    if sites:
      if len( sites ) == 1:
        self.jobLog.info( "Single chosen site %s specified" % ( sites[0] ) )
      else:
        self.jobLog.info( "Multiple sites requested: %s" % ','.join( sites ) )
      sites = self._applySiteFilter( sites, banned = bannedSites )
      if not sites:
        return S_ERROR( "Impossible site requirement" )

    return S_OK( ( sites, bannedSites ) )


  def __sendToTQ( self, jobState, sites, bannedSites ):
    """This method sends jobs to the task queue agent and if candidate sites
       are defined, updates job JDL accordingly.
    """
    result = jobState.getManifest()
    if not result[ 'OK' ]:
      return S_ERROR( "Could not retrieve manifest: %s" % result[ 'Message' ] )
    manifest = result[ 'Value' ]

    reqSection = "JobRequirements"

    if reqSection in manifest:
      result = manifest.getSection( reqSection )
    else:
      result = manifest.createSection( reqSection )
    if not result[ 'OK' ]:
      self.jobLog.error( "Cannot create %s: %s" % reqSection, result[ 'Value' ] )
      return S_ERROR( "Cannot create %s in the manifest" % reqSection )
    reqCfg = result[ 'Value' ]

    if sites:
      reqCfg.setOption( "Sites", ", ".join( sites ) )
    if bannedSites:
      reqCfg.setOption( "BannedSites", ", ".join( bannedSites ) )

    # Job multivalue requirement keys are specified as singles in the job descriptions
    # but for backward compatibility can be also plurals
    for key in ( 'SubmitPools', "SubmitPool", "GridMiddleware", "PilotTypes", "PilotType",
                 "JobType", "GridRequiredCEs", "GridCE", "Tags" ):
      reqKey = key
      if key == "JobType":
        reqKey = "JobTypes"
      elif key == "GridRequiredCEs" or key == "GridCE":
        reqKey = "GridCEs"
      elif key == "SubmitPools" or key == "SubmitPool":
        reqKey = "SubmitPools"
      elif key == "PilotTypes" or key == "PilotType":
        reqKey = "PilotTypes"
      if key in manifest:
        reqCfg.setOption( reqKey, ", ".join( manifest.getOption( key, [] ) ) )

    result = self.__setJobSite( jobState, sites )
    if not result[ 'OK' ]:
      return result

    self.jobLog.info( "Done" )
    return self.setNextOptimizer( jobState )

  def __resolveStaging( self, jobState, inputData, idSites ):
    diskSites = []
    maxOnDisk = 0
    bestSites = []

    for site in idSites:
      nTape = idSites[ site ][ 'tape' ]
      nDisk = idSites[ site ][ 'disk' ]
      if nTape > 0:
        self.jobLog.verbose( "%s tape replicas on site %s" % ( nTape, site ) )
      if nDisk > 0:
        self.jobLog.verbose( "%s disk replicas on site %s" % ( nDisk, site ) )
        if nDisk == len( inputData ):
          diskSites.append( site )
      if nDisk > maxOnDisk:
        maxOnDisk = nDisk
        bestSites = [ site ]
      elif nDisk == maxOnDisk:
        bestSites.append( site )

    # If there are selected sites, those are disk only sites
    if diskSites:
      self.jobLog.info( "No staging required" )
      return ( False, diskSites )

    self.jobLog.info( "Staging required" )
    if len( bestSites ) > 1:
      random.shuffle( bestSites )
    return ( True, bestSites )

  def __preRequestStaging( self, jobState, stageSite, opData ):
    result = getSEsForSite( stageSite )
    if not result['OK']:
      return S_ERROR( 'Could not determine SEs for site %s' % stageSite )
    siteSEs = result['Value']

    tapeSEs = []
    diskSEs = []
    result = jobState.getManifest()
    if not result['OK']:
      return result
    manifest = result['Value']
    vo = manifest.getOption( 'VirtualOrganization' )
    for seName in siteSEs:
      se = StorageElement( seName, vo = vo )
      result = se.getStatus()
      if not result[ 'OK' ]:
        self.jobLog.error( "Cannot retrieve SE %s status: %s" % ( seName, result[ 'Message' ] ) )
        return S_ERROR( "Cannot retrieve SE status" )
      seStatus = result[ 'Value' ]
      if seStatus[ 'Read' ] and seStatus[ 'TapeSE' ]:
        tapeSEs.append( seName )
      if seStatus[ 'Read' ] and seStatus[ 'DiskSE' ]:
        diskSEs.append( seName )

    if not tapeSEs:
      return S_ERROR( "No Local SEs for site %s" % stageSite )

    self.jobLog.verbose( "Tape SEs are %s" % ( ", ".join( tapeSEs ) ) )

    # I swear this is horrible DM code it's not mine.
    # Eternity of hell to the inventor of the Value of Value of Success of...
    inputData = opData['Value']['Value']['Successful']
    stageLFNs = {}
    lfnToStage = []
    for lfn in inputData:
      replicas = inputData[ lfn ]
      # Check SEs
      seStage = []
      for seName in replicas:
        if seName in diskSEs:
          # This lfn is in disk. Skip it
          seStage = []
          break
        if seName not in tapeSEs:
          # This lfn is not in this tape SE. Check next SE
          continue
        seStage.append( seName )
      for seName in seStage:
        if seName not in stageLFNs:
          stageLFNs[ seName ] = []
        stageLFNs[ seName ].append( lfn )
        if lfn not in lfnToStage:
          lfnToStage.append( lfn )

    if not stageLFNs:
      return S_ERROR( "Cannot find tape replicas" )

    # Check if any LFN is in more than one SE
    # If that's the case, try to stage from the SE that has more LFNs to stage to group the request
    # 1.- Get the SEs ordered by ascending replicas
    sortedSEs = reversed( sorted( [ ( len( stageLFNs[ seName ] ), seName ) for seName in stageLFNs.keys() ] ) )
    for lfn in lfnToStage:
      found = False
      # 2.- Traverse the SEs
      for _stageCount, seName in sortedSEs:
        if lfn in stageLFNs[ seName ]:
          # 3.- If first time found, just mark as found. Next time delete the replica from the request
          if found:
            stageLFNs[ seName ].remove( lfn )
          else:
            found = True
        # 4.-If empty SE, remove
        if len( stageLFNs[ seName ] ) == 0:
          stageLFNs.pop( seName )

    return S_OK( stageLFNs )


  def __requestStaging( self, jobState, stageLFNs ):
    """ Actual request for staging LFNs through the StorageManagerClient
    """
    self.jobLog.verbose( "Stage request will be \n\t%s" % "\n\t".join( [ "%s:%s" % ( lfn, stageLFNs[ lfn ] ) for lfn in stageLFNs ] ) )

    stagerClient = StorageManagerClient()
    result = jobState.setStatus( self.ex_getOption( 'StagingStatus', 'Staging' ),
                                 self.ex_getOption( 'StagingMinorStatus', 'Request To Be Sent' ),
                                 appStatus = "",
                                 source = self.ex_optimizerName() )
    if not result[ 'OK' ]:
      return result

    result = stagerClient.setRequest( stageLFNs, 'WorkloadManagement',
                                      'updateJobFromStager@WorkloadManagement/JobStateUpdate',
                                      int( jobState.jid ) )
    if not result[ 'OK' ]:
      self.jobLog.error( "Could not send stage request: %s" % result[ 'Message' ] )
      return S_ERROR( "Problem sending staging request" )

    rid = str( result[ 'Value' ] )
    self.jobLog.info( "Stage request %s sent" % rid )
    jobState.setParameter( "StageRequest", rid )

    result = jobState.setStatus( self.ex_getOption( 'StagingStatus', 'Staging' ),
                                 self.ex_getOption( 'StagingMinorStatus', 'Request Sent' ),
                                 appStatus = "",
                                 source = self.ex_optimizerName() )
    if not result['OK']:
      return result

    return S_OK( stageLFNs )


  def __updateSharedSESites( self, jobState, stageSite, stagedLFNs, opData ):
    siteCandidates = opData[ 'SiteCandidates' ]

    seStatus = {}
    result = jobState.getManifest()
    if not result['OK']:
      return result
    manifest = result['Value']
    vo = manifest.getOption( 'VirtualOrganization' )
    for siteName in siteCandidates:
      if siteName == stageSite:
        continue
      self.jobLog.verbose( "Checking %s for shared SEs" % siteName )
      siteData = siteCandidates[ siteName ]
      result = getSEsForSite( siteName )
      if not result[ 'OK' ]:
        continue
      closeSEs = result[ 'Value' ]
      diskSEs = []
      for seName in closeSEs:
        # If we don't have the SE status get it and store it
        if seName not in seStatus:
          seObj = StorageElement( seName, vo = vo )
          result = seObj.getStatus()
          if not result['OK' ]:
            self.jobLog.error( "Cannot retrieve SE %s status: %s" % ( seName, result[ 'Message' ] ) )
            continue
          seStatus[ seName ] = result[ 'Value' ]
        # get the SE status from mem and add it if its disk
        status = seStatus[ seName ]
        if status['Read'] and status['DiskSE']:
          diskSEs.append( seName )
      self.jobLog.verbose( "Disk SEs for %s are %s" % ( siteName, ", ".join( diskSEs ) ) )

      # Hell again to the dev of this crappy value of value of successful of ...
      lfnData = opData['Value']['Value']['Successful']
      for seName in stagedLFNs:
        # If the SE is not close then skip it
        if seName not in closeSEs:
          continue
        for lfn in stagedLFNs[ seName ]:
          self.jobLog.verbose( "Checking %s for %s" % ( seName, lfn ) )
          # I'm pretty sure that this cannot happen :P
          if lfn not in lfnData:
            continue
          # Check if it's already on disk at the site
          onDisk = False
          for siteSE in lfnData[ lfn ]:
            if siteSE in diskSEs:
              self.jobLog.verbose( "%s on disk for %s" % ( lfn, siteSE ) )
              onDisk = True
          # If not on disk, then update!
          if not onDisk:
            self.jobLog.verbose( "Setting LFN to disk for %s" % ( seName ) )
            siteData[ 'disk' ] += 1
            siteData[ 'tape' ] -= 1

    return S_OK()


  def __setJobSite( self, jobState, siteList ):
    """ Set the site attribute
    """
    numSites = len( siteList )
    if numSites == 0:
      self.jobLog.info( "Any site is candidate" )
      return jobState.setAttribute( "Site", "ANY" )
    elif numSites == 1:
      self.jobLog.info( "Only site %s is candidate" % siteList[0] )
      return jobState.setAttribute( "Site", siteList[0] )

    tierSite = []
    tierLevel = -1
    for siteName in siteList:
      result = getSiteTier( siteName )
      if not result[ 'OK' ]:
        self.jobLog.error( "Cannot get tier for site %s" % ( siteName ) )
        continue
      siteTier = result[ 'Value' ]

      # FIXME: hack for cases where you get a T0 together with T1(s) in the list of sites and you want to see "multiple"
      if siteTier == 0:
        siteTier = 1

      if tierLevel == -1 or tierLevel > siteTier:
        tierLevel = siteTier
        tierSite = []
      if tierLevel == siteTier:
        tierSite.append( siteName )

    if len( tierSite ) == 1:
      siteName = "Group.%s" % ".".join( tierSite[0].split( "." )[1:] )
      self.jobLog.info( "Group %s is candidate" % siteName )
    else:
      siteName = "Multiple"
      self.jobLog.info( "Multiple sites are candidate" )

    return jobState.setAttribute( "Site", siteName )

  def __checkStageAllowed( self, jobState ):
    """Check if the job credentials allow to stage date """
    result = jobState.getAttribute( "OwnerGroup" )
    if not result[ 'OK' ]:
      self.jobLog.error( "Cannot retrieve OwnerGroup from DB: %s" % result[ 'Message' ] )
      return S_ERROR( "Cannot get OwnerGroup" )
    group = result[ 'Value' ]
    return S_OK( Properties.STAGE_ALLOWED in Registry.getPropertiesForGroup( group ) )
