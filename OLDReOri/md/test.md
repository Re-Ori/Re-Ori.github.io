
```cpp
#include <bits/stdc++.h>
using namespace std;
int a,b,n,m,x;
long long upa[32],upxb[32],downa[32],downxb[32],pa[32],pxb[32];

int main(){
	cin>>a>>n>>m>>x;
	pa[1]=a;
	upa[1]=a;
	upxb[2]=1;
	downxb[2]=1;
	pa[2]=a;
	for(int i=3;i<=n-1;i++){
		upa[i]=upa[i-1]+upa[i-2];
		upxb[i]=upxb[i-1]+upxb[i-2];
		downa[i]=upa[i-1];
		downxb[i]=upxb[i-1];
		pa[i]=pa[i-1]+upa[i]-downa[i];
		pxb[i]=pxb[i-1]+upxb[i]-downxb[i-1];	
	}
	b=(m-pa[n-1])/pxb[n-1];
	//cout<<"b=("<<m<<"-"<<pa[n-1]<<")/"<<pxb[n-1]<<"="<<b<<endl;
	pa[n]=0;
	pxb[n]=0;
	//for(int i=1;i<=n;i++){
	//	cout<<pa[i]+b*pxb[i]<<"(up"<<upa[i]+b*upxb[i]<<",down"<<downa[i]+b*downxb[i]<<")"<<endl; 
	//}
	//cout<<pa[x]+b*pxb[x];
	cout<<pa[x]+b*pxb[x]+1;
	return 0;
}
```
