
import boto3
import time
from fabulous.color import bold, green, highlight_red
from pyfiglet import Figlet
from PyInquirer import style_from_dict, Token, prompt,Separator
from PyInquirer import Validator, ValidationError
import sys
from pprint import pformat
from datetime import datetime


style = style_from_dict({
    Token.Separator: '#cc5454',
    Token.QuestionMark: '#673ab7 bold',
    Token.Selected: '#cc5454',  # default
    Token.Pointer: '#673ab7 bold',
    Token.Instruction: '',  # default
    Token.Answer: '#f44336 bold',
    Token.Question: '',
})


def log(obj):
    now = datetime.now().strftime("%m/%d/%Y %H:%M:%S")
    message = f">{now} -- {obj}"
    print(bold(green(message)))

def confirm(string,eject=False):
    questions = [
        {
            'type': 'confirm',
            'message': string,
            'name': 'use',
            "default": False
        }
    ]    
    value = prompt(questions, style=style)["use"]
    if not value and eject:
        log("Ejecting out of this situation...Goodbye.")
        sys.exit()
        return;
    return value

def get_account_id(profile):
     return profile.client('sts').get_caller_identity()["Account"]

def exit_with_error(msg):
    print(msg) 
    sys.exit()

def revoke_grants(session,grants):
    log("Revoking Grants to any KMS keys used.")
    for grant in grants:
        key,grant_id = grant
        client = session.client('kms')
        client.revoke_grant(KeyId=key,GrantId=grant_id)

def grant_kms(session,key,account):
    return session.client('kms').create_grant(
        KeyId=key,
        GranteePrincipal=f"arn:aws:iam::{account}:root",
        Operations=['Decrypt','Encrypt','GenerateDataKey','GenerateDataKeyWithoutPlaintext','ReEncryptFrom','ReEncryptTo','Sign','Verify','CreateGrant','RetireGrant','DescribeKey','GenerateDataKeyPair','GenerateDataKeyPairWithoutPlaintext']
    )["GrantId"]

def describe_instance(session,dst_session, id):
    try:
        log(f"getting information for {id}")
        grant_ids = []
        instance =  session.client('ec2').describe_instances(InstanceIds=[id])["Reservations"][0]["Instances"][0]
        # DECIDE IF ENCRYPTED AND WHETHER KMS IS AWS MANAGED
        instance["Volumes"] = session.client('ec2').describe_volumes(VolumeIds=[b["Ebs"]["VolumeId"] for b in instance["BlockDeviceMappings"]])["Volumes"]        
        for vol in instance["Volumes"]:
            # BREAK IF AWS MANAGED KMS KEY USED OR IF DST ACCOUNT DOES NOT HAVE ACCESS TO KMS KEY
            keys = []
            if "KmsKeyId" in vol:                
                if session.client('kms').describe_key(KeyId=vol["KmsKeyId"])["KeyMetadata"]["KeyManager"] == "AWS":
                    exit_with_error('Unable to teleport instance since it uses AWS Managed KMS for encryption')
                keys.append(vol["KmsKeyId"])
            keys = list(set(keys))            
            if len(keys):
                for key in keys:
                    dst_account = get_account_id(dst_session)
                    grant_id = grant_kms(session,key,dst_account)
                    grant_ids.append((grant_id,key,session))
        tags = map(lambda t: { "Key": t["Key"], "Value": t["Value"] } ,session.client('ec2').describe_tags(Filters=[{'Name': 'resource-id','Values': [id]}])["Tags"])
        instance["Tags"] = list(tags)
        encrypted = True if len(keys) else False
        return (instance,grant_ids,keys,encrypted)
    except Exception as e:
        print(e)
        exit_with_error(f"Couldn't find {id}")

def delete_ami(session,ami,existing):
    session.client('ec2').deregister_image(ImageId=ami)
    for exist in existing:
        snaps = [s["Ebs"]["SnapshotId"] for s in exist["BlockDeviceMappings"]]
        for snap in snaps:
            session.client('ec2').delete_snapshot(SnapshotId=snap)

def create_ami(session,instance):
    instance_id = instance["InstanceId"]    
    name = f"TELEPORT-{instance_id}"
    existing = session.client('ec2').describe_images(Filters=[{"Name": "name", "Values":[name]}])["Images"]
    if len(existing):
        ami_id = existing[0]["ImageId"]
        use = inquire_existing_ami(f"Use existing AMI - {ami_id}")
        if use:
            return ami_id
        delete_ami(session,ami_id,existing)
    log(f"create AMI({name}) for {instance_id}")
    ami = session.client('ec2').create_image(Name=name,Description="USED IN EC2 TELEPORT",InstanceId=instance_id)["ImageId"]
    waiter = session.client('ec2').get_waiter('image_available')
    waiter.wait(ImageIds=[ami],WaiterConfig={"Delay": 20, "MaxAttempts": 120})   
    log(f"AMI({ami}) is available")
    return ami

def describe_ami_blockdevicemappings(session,ami):
    return session.client('ec2').describe_images(ImageIds=[ami])["Images"][0]["BlockDeviceMappings"]

def apply_mappings_edits(mappings,kms):
    # SET KMS KEY TO SELECTED DESTIONATION KMS KEY
    # SET EBS VOLUME TO DELETE ON EC2 TERMINATION
    def map_func(x):
        if kms:
            x["Ebs"]["Encrypted"] = True 
            x["Ebs"]["KmsKeyId"] = kms
        x["Ebs"]["DeleteOnTermination"] = True
        return x
    return list(map(map_func,mappings))

def copy_ami(session,ami,src_region,dst_region,key):
    if src_region == dst_region:
        return ami   
    name = f"TELEPORT-{ami}"
    existing = session.client('ec2').describe_images(Filters=[{"Name": "name", "Values":[name]}])["Images"]
    if len(existing):
        ami_id = existing[0]["ImageId"]
        use = inquire_existing_ami(f"Use existing AMI - {ami_id}")
        if use:
            return ami_id
        delete_ami(session,ami_id,existing)
    name = f"TELEPORT-{ami}"   
    args = {
        "ClientToken":name,
        "Description":name,
        "Name":name,
        "SourceImageId":ami,
        "SourceRegion":src_region
    }
    if key:
        args["Encrypted"] = True
        args["KmsKeyId"] = key
    log(f"Copying AMI to {dst_region} region.") 
    copied_ami = session.client('ec2').copy_image(**args)["ImageId"]     
    log(f'Created {copied_ami} in {dst_region}')                
    waiter = session.client('ec2',region_name=dst_region).get_waiter('image_available')
    waiter.wait(ImageIds=[copied_ami],WaiterConfig={"Delay": 20, "MaxAttempts": 120})   
    return copied_ami

def share_ami(session,ami,src_account,dst_account,dst_region):
    if src_account != dst_account:
        log(f"Sharing AMI to {dst_account}")
        session.client('ec2',region_name=dst_region).modify_image_attribute(ImageId=ami,Attribute='launchPermission',UserIds=[dst_account],OperationType='add',LaunchPermission={ 'Add': [{'UserId': dst_account}]})

def tag_volumes(session,region,id,tags):
    client = session.client('ec2',region_name=region)
    time.sleep(15)
    mappings = client.describe_instances(InstanceIds=[id])["Reservations"][0]["Instances"][0]["BlockDeviceMappings"]
    # sleep for 15 seconds to give volumes chance to come up
    tag_list = [t for t in tags]
    client.create_tags(Resources=[x["Ebs"]["VolumeId"] for x in mappings],Tags=tag_list)

def stop_instance(session,instance):
    instance_id = instance["InstanceId"]
    if instance["State"]["Name"] != "stopped":
        log(f"stopping {instance_id}...")
        client =  session.client('ec2')
        client.stop_instances(InstanceIds=[instance_id])
        waiter = client.get_waiter('instance_stopped')
        waiter.wait(InstanceIds=[instance["InstanceId"]]) 
    log(f"{instance_id} has been stopped")
  
def get_vpc(session,az_id = None):
    """
    get vpc information with subnet info and security group info
    """
    client = session.client("ec2")
    vpcs = client.describe_vpcs()["Vpcs"]
    def mapVpcs(x):
        subnet_filters = [{"Name": "vpc-id", "Values":[x["VpcId"]]}]
        if az_id:
            subnet_filters.append({"Name": "availability-zone-id", "Values": [az_id]})
        subnets = client.describe_subnets(Filters=subnet_filters)["Subnets"]
        sgs =  client.describe_security_groups(Filters=[{"Name": "vpc-id","Values":[x["VpcId"]]}])["SecurityGroups"]
        x["Subnets"] = subnets
        x["SecurityGroups"] = sgs
        return x;    
    if not len(vpcs):
        exit_with_error('No Vpcs in selected region')    
    vpcs = map(mapVpcs, vpcs)
    return list(vpcs)

def inquire_instance_id():
    questions = [
        {
            'type': 'input',
            'name': 'id',
            'message': "What instance id are we teleporting?",
        }
    ]    
    value = prompt(questions, style=style)["id"]
    return value

def inquire_regions(session,msg):
    regions = [{"name": r["RegionName"]} for r in session.client('ec2',region_name="us-east-1").describe_regions()["Regions"]]    
    region_questions = [
        {
            'type': 'checkbox',
            'message': msg,
            'name': 'region',
            'choices': regions
        }
    ]    
    region = prompt(region_questions, style=style)["region"][0]
    return region

def inquire_existing_ami(msg):
    questions = [
        {
            'type': 'confirm',
            'message': msg,
            'name': 'use',
            "default": False
        }
    ]    
    value = prompt(questions, style=style)["use"]
    return value

def inquire_vpc(vpcs):
    def mapVpcs(x):
        name_tag = [i for i in x["Tags"] if i["Key"] == "Name"][0]["Value"]
        name_tag = name_tag if name_tag else "no_name"
        return {
            "name": name_tag + " " + x["VpcId"]
        }
    try:
        vpc_questions = [
            {
                'type': 'checkbox',
                'message': 'Select VPC',
                'name': 'vpc',
                'choices': list(map(mapVpcs,vpcs))
            }
        ]    
        vpc = "".join(prompt(vpc_questions, style=style)["vpc"][0].split()[1:])
        return vpc
    except:
        exit_with_error('Unable to inquire about Vpcs')

def inquire_subnet(subnets):
    def mapSubnets(x):
        name_tag = [i for i in x["Tags"] if i["Key"] == "Name"][0]["Value"]
        name_tag = name_tag if name_tag else "no_name"
        return {
            "name": name_tag + " " + x["SubnetId"]
        }
    subnet_questions = [
        {
            'type': 'checkbox',
            'message': 'Select Subnet',
            'name': 'subnet',
            'choices': list(map(mapSubnets,subnets))
        }
    ]    
    subnet = "".join(prompt(subnet_questions, style=style)["subnet"][0].split()[1:])
    return subnet

def inquire_sg(sgs):
    def mapSgs(x):        
        return {
            "name": x["GroupName"] + " " + x["GroupId"]
        }
    sg_questions = [
        {
            'type': 'checkbox',
            'message': 'Select Security Group',
            'name': 'sg',
            'choices': list(map(mapSgs,sgs))
        }
    ]    
    sg = "".join(prompt(sg_questions, style=style)["sg"][0].split()[1:])
    return sg

def inquire_instance_type():
    questions = [
        {
            'type': 'input',
            'message': 'Input Instance Type. Source instance type will be used otherwise.',
            'name': 'instance_type',
        }
    ]    
    instance_type = prompt(questions, style=style)["instance_type"]
    instance_type = instance_type if instance_type != "" else None
    return instance_type

def inquire_profile(session):
    profiles = session.client("iam").list_instance_profiles()["InstanceProfiles"]
    def mapProfiles(x):        
        return {
            "name": x["InstanceProfileName"] + " " + x["InstanceProfileId"]
        }
    profile_questions = [
        {
            'type': 'checkbox',
            'message': 'Select Instance Profile',
            'name': 'profile',
            'choices': list(map(mapProfiles,profiles))
        }
    ]    
    profile = "".join(prompt(profile_questions, style=style)["profile"][0].split()[0])
    return profile 

def inquire_dedicated_host(session):
    hosts = [{"name" : h["HostId"], 'az_id': h["AvailabilityZoneId"], 'cpu': h["AvailableCapacity"]["AvailableVCpus"], 'family':  h["HostProperties"]["InstanceFamily"]} for h in session.client("ec2").describe_hosts()["Hosts"]]
    hosts_options = [{"name" : f'{h["name"]}, {h["cpu"]} vcpus available, {h["family"]} family type'} for h in hosts]
    host_questions = [
        {
            'type': 'checkbox',
            'message': 'Select Dedicated Host',
            'name': 'host',
            'choices': hosts_options
        }
    ]    
    host = prompt(host_questions, style=style)["host"][0].split(',')[0].strip()
    return [h for h in hosts if h['name'] == host][0]

def inquire_deploy_type():
    deploy_questions = [
        {
            'type': 'checkbox',
            'message': 'Select Deploy Type',
            'name': 'type',
            'choices': [{"name": "dedicated host"}, {"name": "dedicated instance"}, {"name": "on demand"}]
        }
    ]    
    deploy_type = prompt(deploy_questions, style=style)["type"][0]
    return deploy_type 
    
def inquire_kms(session,encrypted):
    continue_inquire = True
    if not encrypted:
        questions = [
            {
                'type': 'confirm',
                'message': "The original instance was not encrypted. Should we encrypt this one?",
                'name': 'use',
                "default": False
            }
        ]    
        value = prompt(questions, style=style)["use"]
        continue_inquire = value
    if not continue_inquire:
        return False
    kms = [(k["KeyId"],session.client('kms').list_aliases(KeyId=k["KeyId"])["Aliases"][0]["AliasName"]) for k in session.client("kms").list_keys()["Keys"]]
    def map_kms(x): 
        id,alias = x       
        return {
            "name": alias + " " + id
        }
    kms_questions = [
        {
            'type': 'checkbox',
            'message': 'Select Destionation Account KMS Key to encrypt with',
            'name': 'kms',
            'choices': list(map(map_kms,kms))
        }
    ]    
    kms = "".join(prompt(kms_questions, style=style)["kms"][0].split()[1:])
    return kms  

def inquire_region_kms(session,dst_account):
    kms = [(k["KeyId"],session.client('kms').list_aliases(KeyId=k["KeyId"])["Aliases"][0]["AliasName"]) for k in session.client("kms").list_keys()["Keys"]]
    def map_kms(x):
        id,alias = x        
        return {
            "name": alias + " " + id
        }
    kms_questions = [
        {
            'type': 'checkbox',
            'message': 'Select Source Account Destionation Region KMS Key',
            'name': 'kms',
            'choices': list(map(map_kms,kms))
        }
    ]    
    kmskey = "".join(prompt(kms_questions, style=style)["kms"][0].split()[1:])
    grant_id = grant_kms(session,kmskey,dst_account)
    return (kmskey,(grant_id,kmskey,session))       

def remove_ami(session,ami):
    log(f"Removing AMI - {ami}")
    session.client('ec2').deregister_image(ImageId=ami)

def remove_snapshots(session,mappings):
    for x in mappings:
        id = x["Ebs"]["SnapshotId"]
        log(f"Removing Snapshot - {id}")
        session.client('ec2').delete_snapshot(SnapshotId=id)

def remove_instance(session,region,instance_id):
    log(f"Terminating instance - {instance_id}")
    client = session.client('ec2',region_name=region)
    client.modify_instance_attribute(InstanceId=instance_id,DisableApiTermination={'Value': False})
    client.terminate_instances(InstanceIds=[instance_id])

def write_title(title):
    f = Figlet(font='slant')
    print(f.renderText(title))

def deploy_instance(session,ami,instanceType,tags,mappings,subnet,security_group,profile,host,deploy_type):
        client = session.client('ec2')
        tenancy = 'host' if deploy_type == "dedicated host" else 'dedicated' if deploy_type == 'dedicated instance' else 'default'
        placement_options = {'Tenancy':tenancy}
        if tenancy == 'host':
            placement_options["HostId"] = host
        result = client.run_instances(
            Placement=placement_options,
            BlockDeviceMappings=mappings,
            ImageId=ami,
            InstanceType=instanceType,
            SecurityGroupIds=[security_group],
            SubnetId=subnet,
            DisableApiTermination=True,
            MaxCount=1,
            MinCount=1,
            IamInstanceProfile={'Name': profile}
            )
        if "Instances" in result and len(result["Instances"]) > 0:
            # CREATE TAGS FOR INSTANCE
            client.create_tags(
                Resources=[result["Instances"][0]["InstanceId"]],
                Tags=tags
            )
            return result["Instances"][0]["InstanceId"]
            
        return "FAIL"

def get_sessions():
    s_reg = inquire_regions(boto3.Session(profile_name="src"),"Select Source Region")
    d_reg = inquire_regions(boto3.Session(profile_name="dst"),"Select Destination Region")
    return (
        boto3.Session(profile_name="src",region_name=s_reg ),
        boto3.Session(profile_name="src",region_name=d_reg),
        boto3.Session(profile_name="dst",region_name=d_reg ),
        s_reg,
        d_reg
    ) 
def get_account_ids(accounts):
    return (x.client('sts').get_caller_identity()["Account"] for x in accounts)
def get_destinfo(sess,encrypted):
    deploy_type = inquire_deploy_type()
    az_id = None
    host = None
    if deploy_type == "dedicated host":
        host = inquire_dedicated_host(sess)
        host = host["name"]
        az_id = host["az_id"]
    vpcs = get_vpc(sess,az_id)    
    vpc = inquire_vpc(vpcs)
    subnet = inquire_subnet([i for i in vpcs if i["VpcId"] == vpc][0]["Subnets"])
    security_group = inquire_sg([i for i in vpcs if i["VpcId"] == vpc][0]["SecurityGroups"])
    profile = inquire_profile(sess)
    kms = inquire_kms(sess,encrypted)
    return (vpc,subnet,security_group,profile,kms,host,deploy_type)

if __name__ == "__main__":
    write_title('EC2 Teleporter')
    # GET ACCOUNT IDS
    (src_pro,src_copy_pro,dst_pro,src_region,dst_region) = get_sessions()
    (src_account,dst_account) = get_account_ids([src_pro,dst_pro]) 
    # VAR FOR DENOTING WHETHER CROSS REGION
    x_region = src_region != dst_region
    # GET SOURCE INSTANCE INFORMATION
    instance_id = inquire_instance_id().strip()
    instance,grant_ids,keys,encrypted = describe_instance(src_pro,dst_pro,instance_id)
    # STOP INSTANCE
    stop_instance(src_pro,instance)  
    # CREATE AMI
    original_ami = create_ami(src_pro, instance) 
    original_mappings = describe_ami_blockdevicemappings(src_pro,original_ami)

    # GET DESTINATION INFORMATION    
    (vpc,subnet,security_group,profile,kms,host,deploy_type) = get_destinfo(dst_pro,encrypted) 
    region_kms,grant = inquire_region_kms(src_copy_pro,dst_account) if x_region and kms != False and src_account != dst_account else (False,None)
    if grant:
        grant_ids.append(grant)
    # DESCRIBE AMI TO GET BLOCKDEVICEMAPPINGS
    ami = copy_ami(src_copy_pro,original_ami,src_region,dst_region,region_kms if region_kms else kms)
    # BASED ON WHETHER COPIED AMI TO NEW REGION 
    mappings = apply_mappings_edits(describe_ami_blockdevicemappings(src_copy_pro if x_region else src_pro,ami),kms)
    share_ami(src_pro,ami,src_account,dst_account,dst_region)
    confirm_dic = {"vpc": vpc, "subnet": subnet, "security_group": security_group,"profile": profile,"kms": kms, 'deploy_type': deploy_type, 'host':host}
    confirm_str = '''\
        Teleporter would like to confirm your selections:
                -----> vpc =            {vpc}
                -----> subnet =         {subnet}
                -----> security_group = {security_group}
                -----> profile =        {profile}
                -----> kms =            {kms}
                -----> deploy type  =   {deploy_type}
                -----> host  =          {host}
          Are these right? Press 'n' to pull the eject cord
        '''.format(**confirm_dic)
    confirm(string=confirm_str,eject=True)
    # DEPLOY INSTANCE IN DESTINATION
    instance_type = inquire_instance_type()
    instance_type = instance_type if instance_type else instance["InstanceType"]
    new_instance = deploy_instance(dst_pro,ami,instance_type,instance["Tags"],mappings,subnet,security_group,profile,host,deploy_type)
    log(f"Instance has been teleported.")
    log(f"Instance id is {new_instance}")
    # TAG ATTACHED EBS VOLUMES
    tag_volumes(dst_pro,dst_region,new_instance,instance["Tags"])
    #REMOVE GRANTS FOR ANY KMS KEYS
    for grant,kms,sess in grant_ids:
        sess.client('kms').revoke_grant(KeyId=kms,GrantId=grant)
    # DELETE SNAPSHOTS AND AMI
    if x_region:
        confirm_str = '''\
            Would you like to cleanup the following resources in the source account destination region?
                    -----> ami =            {ami}
            Press 'y' to teleport them to the shadown realm.
            '''.format(ami=ami)
        should_clean = confirm(confirm_str)
        if should_clean:
            remove_ami(src_copy_pro,ami)
            remove_snapshots(src_copy_pro,mappings)         
    confirm_str = '''\
        Would you like to cleanup the following resources?
                -----> ami =            {ami}
          Press 'y' to teleport them to the shadown realm.
        '''.format(ami=original_ami)
    should_clean = confirm(confirm_str)
    if should_clean:
        remove_ami(src_pro,original_ami)
        remove_snapshots(src_pro,original_mappings) 
    # DELETE ORIGINAL INSTANCE
    confirm_str = '''\
    Would you like to terminate the original instance?
            -----> instance_id =            {instance_id}
        Press 'y' to teleport it to the shadown realm.
    '''.format(instance_id=instance_id)
    should_clean = confirm(confirm_str)
    if should_clean:
        remove_instance(src_pro,src_region,instance_id)
    log("Teleporter has finished")
    log(f"Your new instance is {new_instance}")
    
       
